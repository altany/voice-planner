"""Web server: receives a voice note, turns it into tasks, saves them.

GET  /            — record page (token entered there once)
GET  /health      — unauthenticated liveness check
POST /note        — multipart upload, field name "audio"
GET  /tasks       — task list (?all=1 includes the deleted archive)
PATCH  /tasks/<id> — edit fields / complete / delete (status)
DELETE /tasks/<id> — same as PATCH {"status": "deleted"}
GET  /geocode?q=  — place-name suggestions for the location editor

Everything except / and /health requires Authorization: Bearer <PLANNER_TOKEN>.
"""

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from planner import extract, gcal, geocode, gtasks, storage, sync, transcribe  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("planner")

app = Flask(__name__)
# Voice notes are small; 30 MB leaves headroom without letting a bad upload eat RAM.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

AUTH_TOKEN = os.environ.get("PLANNER_TOKEN", "")

ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".wav", ".webm", ".ogg", ".flac"}

EDITABLE_FIELDS = ("title", "description", "deadline", "deadline_time", "location",
                   "lat", "lon", "maps_url", "status")


def _geocode_fields(location: str | None) -> dict:
    """Resolve a spoken/typed location. When Google Places finds the business,
    the task's location becomes the real address (that's the point: “put the
    address on it”); plain street/city results keep the user's wording."""
    if not location:
        return {"location": None, "lat": None, "lon": None, "maps_url": None}
    place = geocode.lookup(location)
    if not place:
        return {"location": location, "lat": None, "lon": None, "maps_url": None}
    fields = {"location": location, "lat": place["lat"], "lon": place["lon"],
              "maps_url": place.get("maps_url")}
    if place.get("provider") == "google":
        fields["location"] = ", ".join(p.strip() for p in place["label"].split(",")[:3])
    return fields


_sync_calendar = gcal.sync_task


def _authorized(req) -> bool:
    if not AUTH_TOKEN:
        return False  # refuse everything rather than run wide open
    header = req.headers.get("Authorization", "")
    supplied = header.removeprefix("Bearer ").strip()
    return secrets.compare_digest(supplied, AUTH_TOKEN)


def _unauthorized():
    return jsonify({"status": "error", "error": "unauthorized"}), 401


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/tasks")
def tasks_list():
    if not _authorized(request):
        return _unauthorized()
    try:
        sync.reconcile()
    except Exception:
        log.exception("google sync failed; serving local state")
    include_deleted = request.args.get("all") == "1"
    return jsonify({"tasks": storage.list_tasks(include_deleted=include_deleted)})


@app.route("/tasks/<task_id>", methods=["PATCH", "DELETE"])
def tasks_update(task_id):
    if not _authorized(request):
        return _unauthorized()
    task = storage.get_task(task_id)
    if task is None:
        return jsonify({"status": "error", "error": "no such task"}), 404

    if request.method == "DELETE":
        fields = {"status": "deleted"}
    else:
        body = request.get_json(silent=True) or {}
        fields = {k: v for k, v in body.items() if k in EDITABLE_FIELDS}
        if not fields:
            return jsonify({"status": "error", "error": "no editable fields in body"}), 400
        # Location text changed without explicit coordinates → geocode it.
        if "location" in fields and "lat" not in fields:
            fields.update(_geocode_fields(fields["location"]))

    try:
        updated = storage.update_task(task_id, fields)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400

    google_error = None
    if task["google_id"]:
        if updated["status"] == "deleted":
            google_error = gtasks.delete_task(task["google_id"])
        else:
            google_error = gtasks.patch_task(task["google_id"], updated)

    updated, cal_error = _sync_calendar(updated)
    errors = "; ".join(e for e in (google_error, cal_error) if e) or None

    return jsonify({"status": "ok" if not errors else "partial",
                    "task": updated, "google_error": errors})


@app.get("/geocode")
def geocode_search():
    if not _authorized(request):
        return _unauthorized()
    return jsonify({"results": geocode.search(request.args.get("q", ""))})


@app.post("/note")
def note():
    if not _authorized(request):
        return _unauthorized()

    upload = request.files.get("audio")
    if upload is None or upload.filename == "":
        return jsonify({"status": "error", "error": "no file in 'audio' field"}), 400

    suffix = Path(upload.filename).suffix.lower() or ".m4a"
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"status": "error", "error": f"unsupported audio type {suffix}"}), 400

    # Keep the original extension: the OpenAI API uses it to detect the format.
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        upload.save(tmp)
        tmp_path = Path(tmp.name)

    try:
        return _process(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _process(audio_path: Path):
    current = [t for t in storage.list_tasks() if t["status"] == "active"][:50]
    vocab = [t["title"] for t in current] + [t["location"] for t in current if t["location"]]

    try:
        transcript = transcribe.transcribe(audio_path, vocab=vocab)
    except Exception:
        log.exception("transcription failed")
        return jsonify({"status": "error", "error": "transcription failed, see server logs"}), 502

    if not transcript.strip():
        return jsonify({"status": "error", "error": "empty transcript"}), 422

    actions = extract.extract_actions(transcript, current)

    if actions is None:
        # Extraction failed twice — keep the transcript so nothing is lost.
        storage.append_journal(transcript, tasks=None, note="action extraction failed")
        return (
            jsonify(
                {
                    "status": "partial",
                    "transcript": transcript,
                    "error": "could not extract actions; raw transcript saved on the Pi",
                }
            ),
            200,
        )

    added, updated, completed, deleted, warnings = [], [], [], [], []

    for a in actions:
        a["deadline_time"] = a.pop("time", None)
        if a["action"] == "add":
            a.update(_geocode_fields(a.get("location")))
            row = storage.add_tasks([a])[0]
            errors, google_ids = gtasks.add_tasks([row])
            warnings.extend(errors)
            if row["id"] in google_ids:
                storage.update_task(row["id"], {"google_id": google_ids[row["id"]]})
            row, cal_error = _sync_calendar(storage.get_task(row["id"]))
            if cal_error:
                warnings.append(cal_error)
            added.append(row)
            continue

        task = storage.get_task(a["task_id"]) if a["task_id"] else None
        if task is None or task["status"] == "deleted":
            candidates = [storage.get_task(c) for c in a.get("candidates") or []]
            candidates = [c for c in candidates if c and c["status"] != "deleted"]
            if candidates:
                options = "  •  ".join(
                    c["title"] + (f" ({c['deadline']})" if c["deadline"] else "")
                    for c in candidates
                )
                warnings.append(
                    f"which one did you mean? “{a['source']}” matches: {options} "
                    "— say it more specifically (or handle it with ✏️/🗑 on the task itself)"
                )
            else:
                warnings.append(f"couldn't find the task you meant: “{a['source']}”")
            continue

        if a["action"] == "delete":
            result = storage.update_task(task["id"], {"status": "deleted"})
            if task["google_id"]:
                err = gtasks.delete_task(task["google_id"])
                if err:
                    warnings.append(f"{task['title']}: {err}")
            result, cal_error = _sync_calendar(result)
            if cal_error:
                warnings.append(cal_error)
            deleted.append(result)
        elif a["action"] == "complete":
            result = storage.update_task(task["id"], {"status": "completed"})
            if task["google_id"]:
                err = gtasks.patch_task(task["google_id"], result)
                if err:
                    warnings.append(f"{task['title']}: {err}")
            result, cal_error = _sync_calendar(result)
            if cal_error:
                warnings.append(cal_error)
            completed.append(result)
        else:  # update
            fields = {k: a[k] for k in ("title", "deadline", "deadline_time",
                                        "location", "description")
                      if a.get(k) is not None}
            if "location" in fields:
                fields.update(_geocode_fields(fields["location"]))
            result = storage.update_task(task["id"], fields)
            if task["google_id"]:
                err = gtasks.patch_task(task["google_id"], result)
                if err:
                    warnings.append(f"{task['title']}: {err}")
            result, cal_error = _sync_calendar(result)
            if cal_error:
                warnings.append(cal_error)
            updated.append(result)

    storage.append_journal(transcript, tasks=added or None,
                           note=json.dumps(actions, ensure_ascii=False) if actions else None)

    body = {
        "status": "ok" if not warnings else "partial",
        "added": added,
        "updated": updated,
        "completed": completed,
        "deleted": deleted,
        "warnings": warnings,
        "transcript": transcript,
    }
    return jsonify(body), 200


@app.errorhandler(413)
def too_large(_):
    return jsonify({"status": "error", "error": "file too large (30 MB max)"}), 413


def main():
    from waitress import serve

    host = os.environ.get("PLANNER_HOST", "0.0.0.0")
    port = int(os.environ.get("PLANNER_PORT", "8484"))
    if not AUTH_TOKEN:
        log.warning("PLANNER_TOKEN is not set — all requests will be rejected until it is")
    imported = storage.import_legacy_journal()
    if imported:
        log.info("seeded task DB with %d task(s) from the journal", imported)
    log.info("listening on %s:%s", host, port)
    # A couple of worker threads: notes arrive one at a time and this keeps memory flat.
    serve(app, host=host, port=port, threads=2)


if __name__ == "__main__":
    main()

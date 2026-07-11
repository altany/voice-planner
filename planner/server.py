"""Web server: receives a voice note, turns it into tasks, saves them.

POST /note   — multipart upload, field name "audio", Bearer-token protected
GET  /health — unauthenticated liveness check
"""

import logging
import os
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from planner import extract, gtasks, storage, transcribe  # noqa: E402

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


def _authorized(req) -> bool:
    if not AUTH_TOKEN:
        return False  # refuse everything rather than run wide open
    header = req.headers.get("Authorization", "")
    supplied = header.removeprefix("Bearer ").strip()
    return secrets.compare_digest(supplied, AUTH_TOKEN)


@app.get("/")
def index():
    # Record page: open in a phone browser, tap to record, tasks come back.
    # Loading it needs no token (it's tailnet-only); posting a note still does.
    return app.send_static_file("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/tasks")
def tasks():
    if not _authorized(request):
        return jsonify({"status": "error", "error": "unauthorized"}), 401
    return jsonify({"tasks": storage.read_tasks()})


@app.post("/note")
def note():
    if not _authorized(request):
        return jsonify({"status": "error", "error": "unauthorized"}), 401

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
    try:
        transcript = transcribe.transcribe(audio_path)
    except Exception:
        log.exception("transcription failed")
        return jsonify({"status": "error", "error": "transcription failed, see server logs"}), 502

    if not transcript.strip():
        return jsonify({"status": "error", "error": "empty transcript"}), 422

    tasks = extract.extract_tasks(transcript)

    if tasks is None:
        # Extraction failed twice — keep the transcript so nothing is lost.
        storage.append_record(transcript, tasks=None, note="task extraction failed")
        return (
            jsonify(
                {
                    "status": "partial",
                    "transcript": transcript,
                    "error": "could not extract tasks; raw transcript saved on the Pi",
                }
            ),
            200,
        )

    storage.append_record(transcript, tasks=tasks)
    google_errors = gtasks.add_tasks(tasks)

    body = {
        "status": "ok" if not google_errors else "partial",
        "tasks": tasks,
        "saved_locally": True,
        "google_tasks": "ok" if not google_errors else google_errors,
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
    log.info("listening on %s:%s", host, port)
    # One worker thread: notes arrive one at a time and this keeps memory flat.
    serve(app, host=host, port=port, threads=2)


if __name__ == "__main__":
    main()

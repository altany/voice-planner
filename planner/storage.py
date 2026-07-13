"""Task storage.

Two layers:
- data/tasks.jsonl — append-only journal of every voice note (transcript +
  extracted tasks). Never modified, never pruned: this is the archive.
- data/tasks.db (SQLite) — the live task list the page and Google sync work
  against. Deleting a task only flips its status; rows are never removed.
"""

import datetime
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

log = logging.getLogger("planner.storage")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TASKS_FILE = DATA_DIR / "tasks.jsonl"
DB_FILE = DATA_DIR / "tasks.db"

TASK_FIELDS = ("title", "description", "deadline", "deadline_time", "location",
               "lat", "lon", "maps_url", "source")
STATUSES = ("active", "completed", "deleted")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            deadline TEXT,
            deadline_time TEXT,
            location TEXT,
            lat REAL,
            lon REAL,
            maps_url TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            google_id TEXT,
            received_at TEXT,
            updated_at TEXT
        )"""
    )
    # Migrations for DBs created before newer columns existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col in ("deadline_time", "maps_url", "calendar_event_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def append_journal(transcript: str, tasks: list[dict] | None, note: str | None = None) -> None:
    """The immutable per-note archive line; unchanged since v1."""
    record = {
        "received_at": _now(),
        "transcript": transcript,
        "tasks": tasks,
    }
    if note:
        record["note"] = note
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASKS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def add_tasks(tasks: list[dict]) -> list[dict]:
    """Insert extracted tasks as active rows; returns them with ids/timestamps."""
    now = _now()
    rows = []
    with _db() as conn:
        for t in tasks:
            row = {
                "id": uuid.uuid4().hex,
                **{f: t.get(f) for f in TASK_FIELDS},
                "status": "active",
                "google_id": None,
                "received_at": now,
                "updated_at": now,
            }
            conn.execute(
                """INSERT INTO tasks (id, title, description, deadline, deadline_time,
                                      location, lat, lon, maps_url, source, status,
                                      google_id, received_at, updated_at)
                   VALUES (:id, :title, :description, :deadline, :deadline_time,
                           :location, :lat, :lon, :maps_url, :source, :status,
                           :google_id, :received_at, :updated_at)""",
                row,
            )
            rows.append(row)
    return rows


def list_tasks(include_deleted: bool = False) -> list[dict]:
    q = "SELECT * FROM tasks"
    if not include_deleted:
        q += " WHERE status != 'deleted'"
    q += " ORDER BY received_at DESC, rowid DESC"
    with _db() as conn:
        return [_row_to_dict(r) for r in conn.execute(q)]


def get_task(task_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def update_task(task_id: str, fields: dict) -> dict | None:
    """Update editable fields and/or status; bumps updated_at. Returns the row."""
    allowed = {k: v for k, v in fields.items()
               if k in (*TASK_FIELDS, "status", "google_id", "calendar_event_id")}
    if "status" in allowed and allowed["status"] not in STATUSES:
        raise ValueError(f"bad status {allowed['status']!r}")
    if not allowed:
        return get_task(task_id)
    allowed["updated_at"] = _now()
    sets = ", ".join(f"{k} = :{k}" for k in allowed)
    with _db() as conn:
        cur = conn.execute(f"UPDATE tasks SET {sets} WHERE id = :id", {**allowed, "id": task_id})
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def find_unlinked(title: str) -> dict | None:
    """A local task with no google_id and this title — used to link instead of
    duplicating when the first sync meets pre-existing Google tasks."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE google_id IS NULL AND title = ? AND status != 'deleted'",
            (title,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def import_legacy_journal() -> int:
    """One-time: seed the DB from tasks.jsonl if the DB has no rows yet."""
    with _db() as conn:
        if conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
            return 0
    if not TASKS_FILE.exists():
        return 0
    imported = []
    with open(TASKS_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            for t in record.get("tasks") or []:
                imported.append({**t, "received_at": record.get("received_at")})
    count = 0
    with _db() as conn:
        for t in imported:
            conn.execute(
                """INSERT INTO tasks (id, title, description, deadline, location, lat, lon,
                                      source, status, google_id, received_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'active', NULL, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    t.get("title") or "(untitled)",
                    t.get("description"),
                    t.get("deadline"),
                    t.get("location"),
                    t.get("source"),
                    t.get("received_at"),
                    _now(),
                ),
            )
            count += 1
    if count:
        log.info("imported %d task(s) from the legacy journal", count)
    return count

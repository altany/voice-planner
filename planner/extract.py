"""Turn a voice-note transcript into task-list ACTIONS via OpenAI.

The model sees the current task list alongside the transcript, so a note can
add new tasks, but also edit, complete, or cancel existing ones:
"actually, cancel the dentist appointment" → {"action": "delete", task_id: …}.
"""

import datetime
import json
import logging
import os

from openai import OpenAI

log = logging.getLogger("planner.extract")

_client = OpenAI()

MODEL = os.environ.get("TASKS_MODEL", "gpt-4.1-mini")

# strict json_schema mode: the API guarantees the reply matches this shape,
# so parse failures should be rare — but we still guard and retry below.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "task_actions",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "update", "complete", "delete"],
                            },
                            "task_id": {
                                "type": ["string", "null"],
                                "description": "id from CURRENT TASKS; required for update/complete/delete; null for add, when nothing matches, or when several tasks match equally",
                            },
                            "candidates": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "when several CURRENT TASKS match equally well: their ids (task_id stays null); otherwise empty",
                            },
                            "title": {
                                "type": ["string", "null"],
                                "description": "add: short imperative title (required); update: new title only if renaming, else null",
                            },
                            "deadline": {
                                "type": ["string", "null"],
                                "description": "YYYY-MM-DD if mentioned; update: null = leave unchanged",
                            },
                            "time": {
                                "type": ["string", "null"],
                                "description": "HH:MM 24-hour clock, only if a time of day was mentioned; update: null = leave unchanged",
                            },
                            "location": {
                                "type": ["string", "null"],
                                "description": "place tied to the task if mentioned; update: null = leave unchanged",
                            },
                            "description": {
                                "type": ["string", "null"],
                                "description": "extra useful details beyond the title; update: null = leave unchanged",
                            },
                            "source": {
                                "type": "string",
                                "description": "the part of the transcript this action came from",
                            },
                        },
                        "required": ["action", "task_id", "candidates", "title",
                                     "deadline", "time", "location", "description", "source"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """\
You manage a personal to-do list from voice notes. You get the CURRENT TASKS
(with ids) and a new voice-note transcript. Return the list actions the
speaker wants, in the order spoken.

Actions:
- "add": a new task. title is required: short and imperative ("Book dentist
  appointment"), not a full sentence. Ignore filler and thinking out loud.
  "Another X" / "one more X" / "a second X" is always a NEW add — even when a
  similar task exists — never an update of the existing one.
- "update": change an existing task (new date, place, details, or rename).
  Set ONLY the fields being changed; leave every other field null.
- "complete": ONLY when the speaker says it already happened, in the past
  ("I did it", "picked it up", "το έκανα", "τον πήρα", "done"). Scheduling
  words with a day or date — "put/set/move it (to) today/Monday", "βάλε/βάλτο
  σήμερα/τη Δευτέρα" — are ALWAYS an update of the deadline, never complete.
- "delete": the speaker cancels it / no longer wants it ("cancel", "ακύρωσε").
  Cancelling never means rescheduling: don't turn a cancellation into update.

Voice transcripts contain mis-heard words. If a sentence is garbled and you
cannot tell what the speaker wants done to which task, output nothing for it
(or task_id null if the target is clear but the intent isn't) — never guess
an action from a garbled sentence.

Matching rules for update/complete/delete:
- task_id MUST be an id from CURRENT TASKS. Match by meaning, not exact words
  ("the dentist thing" → "Book dentist appointment").
- If the speaker explicitly means ALL of them ("both", "all of them", "και τα
  δύο", "όλα"), emit ONE action PER matching task, each with its task_id —
  no candidates, no question.
- If SEVERAL tasks match equally well (e.g. two dentist appointments and the
  speaker didn't say which), do NOT pick one: leave task_id null and put their
  ids in candidates. Only pick a single task when the speaker's words single
  it out (a date, place, or detail narrows it down, or only one exists).
- candidates must obey the same-subject rule too: only tasks about the SAME
  subject as the spoken phrase. A dentist phrase can never have a phone-call
  task as a candidate. If no same-subject task exists, candidates stays empty
  and task_id null (not found).
- The match must be about the SAME subject. "Cancel the batteries" can only
  match a task about batteries — never an unrelated task, no matter what.
  It is always better to do nothing than to touch the wrong task.
- If nothing in CURRENT TASKS is about that subject, still OUTPUT the action
  with task_id null and candidates empty (so the speaker gets told it wasn't
  found) — but do NOT invent a task and do NOT pick an unrelated one.
- Exception: if the speaker cancels something they added EARLIER IN THIS SAME
  NOTE ("buy batteries… actually forget the batteries"), that's the same-note
  cancellation rule below — output nothing at all for it, no delete action.

Field rules:
- deadline: only if the speaker mentioned one. Resolve relative dates using
  the CALENDAR given below — look the weekday up there, do not compute it
  yourself. A bare weekday ("Thursday") means the next occurrence of that
  weekday. Weeks start on Monday: "next week" begins next Monday, "early next
  week" means Monday or Tuesday of that week, "end of the week" means Friday.
  Never invent a deadline.
- time: only if the speaker named a time of day. Convert to 24-hour HH:MM
  ("5 το απόγευμα" → 17:00, "half past nine in the morning" → 09:30, "μεσημέρι"
  → 12:00). Never invent a time.
- location: the place tied to the task if one was mentioned, phrased the way
  a map search would find it: venue/business/practice name and/or street, plus
  the city when spoken ("στου Παπαδάκη του οδοντίατρου στη Λάρισα" →
  "Παπαδάκης οδοντίατρος, Λάρισα"; "στην Ερμού στην Αθήνα" → "Ερμού, Αθήνα").
  Purely personal references that no map knows ("at mom's place") stay out of
  location. Never invent a place. When CORRECTING a task's location, keep the
  parts the speaker didn't change — especially the city: if the task says
  "…, Λάρισα" and they only fix the name, the new location keeps ", Λάρισα".
- description: extra useful details beyond the title — names, amounts,
  reasons, specifics. One or two short phrases in the speaker's language.
  Do not repeat the title or copy the source verbatim.
- source: quote the part of the transcript the action came from.

If the speaker adds something and then changes their mind about it WITHIN THE
SAME note ("book a table… actually no, forget that"), output nothing for it.
If the note contains no actionable items, return an empty list.
"""


def extract_actions(transcript: str, current_tasks: list[dict]) -> list[dict] | None:
    """Return a list of action dicts, or None if extraction failed twice."""
    today = datetime.date.today()
    calendar = "\n".join(
        f"  {(today + datetime.timedelta(days=i)).strftime('%A')} = "
        f"{(today + datetime.timedelta(days=i)).isoformat()}"
        + ("  (today)" if i == 0 else "")
        for i in range(15)
    )
    task_lines = [
        {k: t[k] for k in ("id", "title", "deadline", "location") if t.get(k)}
        for t in current_tasks
    ]
    user_msg = (
        f"CALENDAR (today and the next 14 days):\n{calendar}\n\n"
        f"CURRENT TASKS:\n{json.dumps(task_lines, ensure_ascii=False)}\n\n"
        f"Transcript:\n{transcript}"
    )

    valid_ids = {t["id"] for t in current_tasks}
    for attempt in (1, 2):
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format=RESPONSE_FORMAT,
            )
            actions = json.loads(resp.choices[0].message.content)["actions"]
            if not isinstance(actions, list):
                raise ValueError("'actions' is not a list")
            for a in actions:
                if a["action"] == "add" and not a.get("title"):
                    raise ValueError("add action missing title")
                if a.get("task_id") and a["task_id"] not in valid_ids:
                    a["task_id"] = None  # hallucinated id → treated as unmatched
                a["candidates"] = [c for c in a.get("candidates") or [] if c in valid_ids]
            return actions
        except Exception:
            log.exception("action extraction attempt %d failed", attempt)

    return None

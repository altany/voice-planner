"""Turn a rambling transcript into a structured task list via OpenAI."""

import datetime
import json
import logging
import os

from openai import OpenAI

log = logging.getLogger("planner.extract")

_client = OpenAI()

MODEL = os.environ.get("TASKS_MODEL", "gpt-4o-mini")

# strict json_schema mode: the API guarantees the reply matches this shape,
# so parse failures should be rare — but we still guard and retry below.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "task_list",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short imperative title, max ~8 words",
                            },
                            "deadline": {
                                "type": ["string", "null"],
                                "description": "YYYY-MM-DD if a deadline was mentioned, else null",
                            },
                            "location": {
                                "type": ["string", "null"],
                                "description": "Place tied to the task (shop, address, city) if mentioned, else null",
                            },
                            "description": {
                                "type": ["string", "null"],
                                "description": "Extra useful details beyond the title (names, quantities, context), else null",
                            },
                            "source": {
                                "type": "string",
                                "description": "The sentence(s) from the transcript this task came from",
                            },
                        },
                        "required": ["title", "deadline", "location", "description", "source"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """\
You turn a rambling voice-note transcript into a clean to-do list.

Rules:
- One entry per distinct task. Ignore filler, greetings, and thinking out loud.
- title: short and imperative ("Book dentist appointment"), not a full sentence.
- deadline: only if the speaker mentioned one. Resolve relative dates ("Friday",
  "end of the month") to a concrete YYYY-MM-DD using today's date given below.
  Weeks start on Monday: "next week" begins next Monday, "early next week"
  means Monday or Tuesday of that week, "end of the week" means Friday.
  If no deadline was mentioned, use null. Never invent a deadline.
- location: a physical place tied to the task (shop, address, neighbourhood,
  city) if the speaker mentioned one, else null. Never invent one.
- description: extra useful details the speaker gave beyond the title — names,
  amounts, reasons, specifics ("wants a cleaning, ask about Saturday slots").
  Keep it to one or two short phrases in the language the speaker used.
  Do not repeat the title or copy the source verbatim; null if nothing extra.
- source: quote the part of the transcript the task came from.
- If the transcript contains no tasks at all, return an empty list.
"""


def extract_tasks(transcript: str) -> list[dict] | None:
    """Return a list of {title, deadline, source} dicts, or None if extraction failed."""
    today = datetime.date.today()
    user_msg = (
        f"Today is {today.strftime('%A')}, {today.isoformat()}.\n\n"
        f"Transcript:\n{transcript}"
    )

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
            data = json.loads(resp.choices[0].message.content)
            tasks = data["tasks"]
            if not isinstance(tasks, list):
                raise ValueError("'tasks' is not a list")
            for t in tasks:
                if not t.get("title"):
                    raise ValueError("task missing title")
            return tasks
        except Exception:
            log.exception("task extraction attempt %d failed", attempt)

    return None

"""Speech to text via OpenAI."""

import os
from pathlib import Path

from openai import OpenAI

_client = OpenAI()  # reads OPENAI_API_KEY from the environment

MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

# Names the transcriber should recognise even in mumbly audio, on top of the
# ones taken from the current task list (e.g. people and shops you mention a
# lot). Comma-separated in .env: PLANNER_VOCAB=Παπαδάκης, Χαλάνδρι
EXTRA_VOCAB = os.environ.get("PLANNER_VOCAB", "")


def transcribe(audio_path: Path, vocab: list[str] | None = None) -> str:
    """vocab: proper nouns likely to appear (task titles, places). The API's
    prompt parameter biases recognition toward them — this is what keeps
    "Παπαδάκης" from coming out as "Παπαδράκης"."""
    words = ", ".join(dict.fromkeys(filter(None, [EXTRA_VOCAB, *(vocab or [])])))
    kwargs = {"prompt": f"Ονόματα και μέρη που ίσως αναφέρω: {words}"[:900]} if words else {}
    with open(audio_path, "rb") as f:
        result = _client.audio.transcriptions.create(model=MODEL, file=f, **kwargs)
    return result.text

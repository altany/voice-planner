"""Speech to text via OpenAI."""

import os
from pathlib import Path

from openai import OpenAI

_client = OpenAI()  # reads OPENAI_API_KEY from the environment

MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")


def transcribe(audio_path: Path) -> str:
    with open(audio_path, "rb") as f:
        result = _client.audio.transcriptions.create(model=MODEL, file=f)
    return result.text

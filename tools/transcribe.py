"""Turn a voice note into text the bookkeeping assistant can act on.

Transcribing first (rather than handing audio straight to the agent) means the words
are stored in conversation memory, can be echoed back so the user can catch a
mishearing, and go through exactly the same path — and evals — as typed messages.
"""
import base64
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import llm

# what Gemini accepts, keyed by what WhatsApp sends (parameters like "; codecs=opus" stripped)
SUPPORTED_AUDIO = {
    "audio/ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "audio/mpeg": "audio/mp3",
    "audio/mp3": "audio/mp3",
    "audio/aac": "audio/aac",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/flac": "audio/flac",
}

INSTRUCTIONS = """You transcribe voice notes sent by Nigerian traders to their bookkeeping assistant.

The speaker may use English, Nigerian Pidgin, Yoruba, Hausa, Igbo, or a mix. Transcribe exactly what was said,
in the language it was said — do not tidy Pidgin into standard English.

- Write money amounts as digits the way traders write them: "fifteen thousand" → "15k", "one million two hundred" → "1.2m",
  "₦2,500" → "2,500". Keep units and quantities ("3 bags", "2 cartons").
- Keep names exactly as heard ("Mama Chidi", "Alhaji Musa").
- If the language is Yoruba, Hausa, Igbo or another non-English language, also give a faithful English translation.
  For English or Pidgin, leave the translation empty.
- If there is no intelligible speech (silence, noise, too faint), set audible to false and leave the transcript empty.
Never invent words you did not hear."""


class Transcript(BaseModel):
    audible: bool = Field(description="False if there was no intelligible speech")
    language: Literal["english", "pidgin", "yoruba", "hausa", "igbo", "mixed", "other"]
    transcript: str = Field(description="What was said, in the language it was said")
    english: str | None = Field(default=None, description="English translation, only for non-English, non-Pidgin speech")


class UnsupportedAudio(ValueError):
    pass


def normalize_mime(mime_type: str) -> str:
    base = mime_type.split(";")[0].strip().lower()
    if base not in SUPPORTED_AUDIO:
        raise UnsupportedAudio(base)
    return SUPPORTED_AUDIO[base]


async def transcribe(audio: bytes, mime_type: str) -> Transcript:
    model = llm.get_transcription_model().with_structured_output(Transcript)
    message = HumanMessage(content=[
        {"type": "text", "text": "Transcribe this voice note."},
        {"type": "audio", "base64": base64.b64encode(audio).decode(), "mime_type": normalize_mime(mime_type)},
    ])
    return await model.ainvoke([SystemMessage(content=INSTRUCTIONS), message])


def as_message(t: Transcript) -> str:
    """The text handed to the assistant (and kept in memory) for a voice note."""
    text = f"(voice note) {t.transcript.strip()}"
    if t.english and t.language not in ("english", "pidgin"):
        text += f"\n(English: {t.english.strip()})"
    return text


if __name__ == "__main__":
    # try it on real recordings:  uv run python -m tools.transcribe note1.ogg note2.ogg
    import asyncio
    import mimetypes
    import sys

    async def _main(paths: list[str]) -> None:
        for path in paths:
            mime = mimetypes.guess_type(path)[0] or "audio/ogg"
            with open(path, "rb") as f:
                t = await transcribe(f.read(), mime)
            print(f"{path}\n  language: {t.language}  audible: {t.audible}\n  assistant sees: {as_message(t)!r}\n")

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_main(sys.argv[1:]))

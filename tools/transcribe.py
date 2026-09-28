"""Turn a voice note into text the bookkeeping assistant can act on.

Transcribing first (rather than handing audio straight to the agent) means the words
are stored in conversation memory, can be echoed back so the user can catch a
mishearing, and go through exactly the same path — and evals — as typed messages.
"""
import base64
import os
from typing import Literal

import httpx

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import llm

# what the transcription models accept, keyed by what WhatsApp sends (parameters like
# "; codecs=opus" stripped)
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
    model_name = llm.transcription_model_name()
    if model_name.startswith("groq:"):
        return await _transcribe_with_groq_whisper(audio, mime_type, model_name.removeprefix("groq:"))
    return await _transcribe_with_chat_model(audio, mime_type)


async def _transcribe_with_chat_model(audio: bytes, mime_type: str) -> Transcript:
    """A multimodal chat model (e.g. Gemini) listens to the audio and fills in Transcript."""
    model = llm.get_audio_chat_model().with_structured_output(Transcript)
    message = HumanMessage(content=[
        {"type": "text", "text": "Transcribe this voice note."},
        {"type": "audio", "base64": base64.b64encode(audio).decode(), "mime_type": normalize_mime(mime_type)},
    ])
    return await model.ainvoke([SystemMessage(content=INSTRUCTIONS), message])


# ── Groq Whisper (speech-to-text API, not a chat model) ──────────────────────

GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio"
# Whisper picks the file type from the name
_EXTENSION = {"audio/ogg": "ogg", "audio/mp3": "mp3", "audio/aac": "m4a", "audio/wav": "wav", "audio/flac": "flac"}
# Nudges spelling towards traders' vocabulary. A word list, not example sentences: Whisper
# copies a prompt's phrasing too (an example "he pay 30k" turned a spoken "he paid" into "he pay").
WHISPER_PROMPT = "Naira amounts: 5k, 45k, 1.5m. Names: Mama Chidi, Emeka, Alhaji, Tunde. Balance, on credit, carton."
# Whisper's language names → ours; Whisper has no Pidgin (it hears it as English) and no Igbo
_LANGUAGES = {"english": "english", "yoruba": "yoruba", "hausa": "hausa"}


async def _transcribe_with_groq_whisper(audio: bytes, mime_type: str, model: str) -> Transcript:
    mime = normalize_mime(mime_type)
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    upload = {"file": (f"voice-note.{_EXTENSION.get(mime, 'ogg')}", audio, mime)}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{GROQ_AUDIO_URL}/transcriptions", headers=headers, files=upload, data={
            "model": model, "response_format": "verbose_json", "temperature": "0", "prompt": WHISPER_PROMPT,
        })
        response.raise_for_status()
        heard = response.json()
        text = (heard.get("text") or "").strip()
        language = _LANGUAGES.get((heard.get("language") or "").lower(), "other")

        english = None
        if text and language != "english":
            # Whisper's translate endpoint turns the same audio into English
            translated = await client.post(f"{GROQ_AUDIO_URL}/translations", headers=headers, files=upload,
                                           data={"model": model, "response_format": "json", "temperature": "0"})
            translated.raise_for_status()
            english = (translated.json().get("text") or "").strip() or None

    return Transcript(audible=bool(text), language=language, transcript=text, english=english)


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

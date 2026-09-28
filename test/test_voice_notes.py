"""
Voice notes over WhatsApp: download, transcription, and how the result reaches the
assistant. Meta's API and the transcription model are stubbed.
"""
import base64

import httpx
import pytest
from langchain_google_genai.chat_models import _convert_to_parts

from channels import whatsapp
from tools import transcribe as voice
from tools.transcribe import Transcript

# reuse the WhatsApp test fixtures and helpers
from test.test_whatsapp import (  # noqa: F401
    ADA_WA, PHONE_NUMBER_ID, ada, agent, client, payload, post_signed, sent, whatsapp_configured,
)


def voice_message(wamid="wamid.voice", mime="audio/ogg; codecs=opus") -> dict:
    return {"from": ADA_WA, "id": wamid, "timestamp": "1790000000", "type": "audio",
            "audio": {"id": "media-1", "mime_type": mime, "voice": True}}


@pytest.fixture
def heard(monkeypatch):
    """Controls the stubbed download + transcription. Set `heard.transcript` to what the
    model 'hears', or `heard.download_error` / `heard.transcribe_error` to fail a step."""
    class Heard:
        transcript = Transcript(audible=True, language="pidgin", transcript="I sell 3 bags rice to Emeka 45k")
        download_error: Exception | None = None
        transcribe_error: Exception | None = None
        downloads: list[str] = []

    async def download_media(media_id, max_bytes=whatsapp.MAX_VOICE_BYTES):
        Heard.downloads.append(media_id)
        if Heard.download_error:
            raise Heard.download_error
        return b"OggS..."

    async def transcribe(audio, mime_type):
        if Heard.transcribe_error:
            raise Heard.transcribe_error
        return Heard.transcript

    Heard.downloads = []
    monkeypatch.setattr(whatsapp, "download_media", download_media)
    monkeypatch.setattr(voice, "transcribe", transcribe)
    return Heard


# ---------- End to end over the webhook ----------

async def test_voice_note_is_recorded_like_a_typed_message(client, ada, agent, sent, heard):
    agent.reply = "Recorded: sold 3 bags of rice to Emeka for ₦45,000."
    await post_signed(client, payload(voice_message()))

    assert agent.calls[0]["message"] == "(voice note) I sell 3 bags rice to Emeka 45k"
    assert sent == [(ADA_WA, "🎤 “I sell 3 bags rice to Emeka 45k”\n\n"
                             "Recorded: sold 3 bags of rice to Emeka for ₦45,000.")]


async def test_translation_is_passed_along_for_yoruba(client, ada, agent, sent, heard):
    heard.transcript = Transcript(audible=True, language="yoruba",
                                  transcript="Mo ta apo iresi meta fun Emeka, 45k",
                                  english="I sold three bags of rice to Emeka, 45k")
    await post_signed(client, payload(voice_message()))
    assert agent.calls[0]["message"] == ("(voice note) Mo ta apo iresi meta fun Emeka, 45k\n"
                                         "(English: I sold three bags of rice to Emeka, 45k)")


async def test_voice_note_is_remembered_for_follow_ups(client, ada, agent, sent, heard):
    await post_signed(client, payload(voice_message(wamid="wamid.v1")))
    await post_signed(client, payload({"from": ADA_WA, "id": "wamid.t2", "type": "text", "text": {"body": "cancel that"}}))
    assert agent.calls[1]["history"][0] == {"role": "user", "content": "(voice note) I sell 3 bags rice to Emeka 45k"}


async def test_inaudible_voice_note_asks_to_resend(client, ada, agent, sent, heard):
    heard.transcript = Transcript(audible=False, language="other", transcript="")
    await post_signed(client, payload(voice_message()))
    assert sent == [(ADA_WA, whatsapp.VOICE_INAUDIBLE_REPLY)]
    assert agent.calls == []


async def test_unsupported_audio_format_is_rejected_before_download(client, ada, agent, sent, heard):
    await post_signed(client, payload(voice_message(mime="audio/amr")))
    assert sent == [(ADA_WA, whatsapp.VOICE_FORMAT_REPLY)]
    assert heard.downloads == []


async def test_long_voice_note_is_declined(client, ada, agent, sent, heard):
    heard.download_error = whatsapp.MediaTooLarge()
    await post_signed(client, payload(voice_message()))
    assert sent == [(ADA_WA, whatsapp.VOICE_TOO_LONG_REPLY)]
    assert agent.calls == []


async def test_download_failure_gets_a_friendly_reply(client, ada, agent, sent, heard):
    heard.download_error = httpx.ConnectError("boom")
    await post_signed(client, payload(voice_message()))
    assert sent == [(ADA_WA, whatsapp.VOICE_FAILED_REPLY)]


async def test_transcription_failure_gets_a_friendly_reply(client, ada, agent, sent, heard):
    heard.transcribe_error = RuntimeError("model down")
    await post_signed(client, payload(voice_message()))
    assert sent == [(ADA_WA, whatsapp.VOICE_FAILED_REPLY)]
    assert agent.calls == []


def test_long_transcript_is_shortened_in_the_echo():
    reply = whatsapp._with_heard("word " * 100, "Recorded.")
    echo = reply.split("\n\n")[0]
    assert len(echo) < whatsapp.HEARD_PREVIEW_CHARS + 10 and echo.endswith("…”")


# ---------- Media download ----------

@pytest.fixture
def graph(monkeypatch):
    """Fake Graph API: media metadata, then the file itself."""
    class Graph:
        requests: list[httpx.Request] = []
        file_size = 1200
        body = b"OggS voice"

    def handler(request: httpx.Request) -> httpx.Response:
        Graph.requests.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media/abc", "mime_type": "audio/ogg",
                                             "file_size": Graph.file_size, "id": "media-1"})
        return httpx.Response(200, content=Graph.body)

    Graph.requests = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return Graph


async def test_download_media_resolves_id_then_fetches_with_token(graph):
    data = await whatsapp.download_media("media-1")
    assert data == b"OggS voice"
    meta, media = graph.requests
    assert str(meta.url).endswith("/media-1")
    assert str(media.url) == "https://lookaside.fbsbx.com/media/abc"
    assert meta.headers["authorization"] == media.headers["authorization"] == "Bearer token"


async def test_download_media_refuses_large_files_without_fetching_them(graph):
    graph.file_size = whatsapp.MAX_VOICE_BYTES + 1
    with pytest.raises(whatsapp.MediaTooLarge):
        await whatsapp.download_media("media-1")
    assert len(graph.requests) == 1


async def test_download_media_checks_actual_size_too(graph):
    graph.body = b"x" * 50
    with pytest.raises(whatsapp.MediaTooLarge):
        await whatsapp.download_media("media-1", max_bytes=10)


# ---------- Transcription request ----------

async def test_transcribe_sends_audio_to_the_model(monkeypatch):
    captured = {}

    class FakeStructured:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return Transcript(audible=True, language="pidgin", transcript="I sell rice 5k")

    class FakeModel:
        def with_structured_output(self, schema):
            captured["schema"] = schema
            return FakeStructured()

    monkeypatch.setenv("TRANSCRIPTION_MODEL", "google_genai:gemini-3.8-flash")  # the listen-directly path
    monkeypatch.setattr(voice.llm, "get_audio_chat_model", lambda: FakeModel())

    result = await voice.transcribe(b"OggS voice", "audio/ogg; codecs=opus")

    assert result.transcript == "I sell rice 5k"
    assert captured["schema"] is Transcript
    system, human = captured["messages"]
    assert "Nigerian Pidgin" in system.content
    audio_block = human.content[1]
    assert audio_block["mime_type"] == "audio/ogg"
    assert base64.b64decode(audio_block["base64"]) == b"OggS voice"
    # the real Gemini integration turns this block into inline audio
    part = _convert_to_parts(human.content)[1]
    assert part.inline_data.mime_type == "audio/ogg" and part.inline_data.data == b"OggS voice"


@pytest.mark.parametrize("given, expected", [
    ("audio/ogg; codecs=opus", "audio/ogg"),
    ("audio/mpeg", "audio/mp3"),
    ("AUDIO/AAC", "audio/aac"),
])
def test_supported_audio_types(given, expected):
    assert voice.normalize_mime(given) == expected


@pytest.mark.parametrize("given", ["audio/amr", "audio/webm", "image/jpeg", ""])
def test_unsupported_audio_types(given):
    with pytest.raises(voice.UnsupportedAudio):
        voice.normalize_mime(given)


# ---------- Groq Whisper (speech-to-text API) ----------

@pytest.fixture
def whisper(monkeypatch):
    """Fake Groq audio API. Set `whisper.heard` to the transcription response JSON."""
    class Whisper:
        requests: list[httpx.Request] = []
        heard = {"text": "I sell 3 bags rice to Emeka 45k", "language": "english"}
        translation = {"text": "I sold three bags of rice to Emeka, 45k"}

    def handler(request: httpx.Request) -> httpx.Response:
        Whisper.requests.append(request)
        if request.url.path.endswith("/transcriptions"):
            return httpx.Response(200, json=Whisper.heard)
        return httpx.Response(200, json=Whisper.translation)

    Whisper.requests = []
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "groq:whisper-large-v3")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return Whisper


async def test_whisper_transcription_request(whisper):
    result = await voice.transcribe(b"OggS voice", "audio/ogg; codecs=opus")

    assert result == Transcript(audible=True, language="english", transcript="I sell 3 bags rice to Emeka 45k")
    [request] = whisper.requests  # English: no translation call
    assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer gsk_test"
    body = request.content
    assert b'filename="voice-note.ogg"' in body and b"OggS voice" in body
    assert b"whisper-large-v3" in body and b"verbose_json" in body
    assert b"Mama Chidi" in body  # the vocabulary prompt


async def test_whisper_translates_yoruba(whisper):
    whisper.heard = {"text": "Mo ta apo iresi meta fun Emeka", "language": "yoruba"}
    result = await voice.transcribe(b"OggS voice", "audio/ogg")
    assert result.language == "yoruba"
    assert result.english == "I sold three bags of rice to Emeka, 45k"
    assert [r.url.path for r in whisper.requests] == ["/openai/v1/audio/transcriptions",
                                                     "/openai/v1/audio/translations"]
    assert voice.as_message(result).endswith("(English: I sold three bags of rice to Emeka, 45k)")


async def test_whisper_silence_is_inaudible(whisper):
    whisper.heard = {"text": "  ", "language": "english"}
    result = await voice.transcribe(b"OggS voice", "audio/ogg")
    assert result.audible is False
    assert len(whisper.requests) == 1  # nothing to translate


async def test_whisper_language_it_does_not_know_is_other(whisper):
    whisper.heard = {"text": "Ahịa m dị mma", "language": "swahili"}  # e.g. Igbo misheard
    result = await voice.transcribe(b"OggS voice", "audio/ogg")
    assert result.language == "other" and result.english is not None

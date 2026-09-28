"""The one place that decides which models the bookkeeper uses. Switch providers with
env vars only — `provider:model`, as LangChain's init_chat_model takes them.

    LLM_MODEL            the agent, and parsing forwarded alert text
                         default groq:openai/gpt-oss-120b         (GROQ_API_KEY)
                         e.g.    google_genai:gemini-3.8-flash    (GOOGLE_API_KEY)
    VISION_MODEL         reading alert screenshots; must accept images
                         default groq:qwen/qwen3.8-27b
    TRANSCRIPTION_MODEL  voice notes (see tools/transcribe.py)
                         default groq:whisper-large-v3  — Groq's speech-to-text API
                         or a chat model that accepts audio, e.g. google_genai:gemini-3.8-flash

LLM_REQUESTS_PER_MINUTE (optional) paces all chat-model calls from this process to stay
under a provider quota. Calls wait rather than fail.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

load_dotenv()

DEFAULT_MODEL = "groq:openai/gpt-oss-120b"
DEFAULT_VISION_MODEL = "groq:qwen/qwen3.8-27b"
DEFAULT_TRANSCRIPTION_MODEL = "groq:whisper-large-v3"


@lru_cache(maxsize=1)
def _rate_limiter() -> InMemoryRateLimiter | None:
    rpm = os.getenv("LLM_REQUESTS_PER_MINUTE")
    if not rpm:
        return None
    # one shared bucket, so all model calls together stay under the limit
    return InMemoryRateLimiter(requests_per_second=float(rpm) / 60, check_every_n_seconds=0.2, max_bucket_size=1)


def _build(model: str, timeout: int) -> BaseChatModel:
    if model.startswith("google_genai:") and not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    return init_chat_model(model, temperature=0, timeout=timeout, max_retries=2, rate_limiter=_rate_limiter())


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    return _build(os.getenv("LLM_MODEL", DEFAULT_MODEL), timeout=30)


@lru_cache(maxsize=1)
def get_vision_model() -> BaseChatModel:
    """Reads alert screenshots. Separate from the chat model because it must accept images."""
    return _build(os.getenv("VISION_MODEL", DEFAULT_VISION_MODEL), timeout=60)


def transcription_model_name() -> str:
    return os.getenv("TRANSCRIPTION_MODEL", DEFAULT_TRANSCRIPTION_MODEL)


@lru_cache(maxsize=1)
def get_audio_chat_model() -> BaseChatModel:
    """A chat model that listens to audio directly (e.g. Gemini) — used for voice notes when
    TRANSCRIPTION_MODEL isn't a Groq Whisper model."""
    return _build(transcription_model_name(), timeout=60)

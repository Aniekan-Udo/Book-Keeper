"""The one place that decides which models the bookkeeper uses. Switch providers with
env vars only — `provider:model`, as LangChain's init_chat_model takes them.

    LLM_MODEL            the agent, and parsing forwarded alert text
                         default groq:openai/gpt-oss-120b         (GROQ_API_KEY)
                         e.g.    openrouter:openai/gpt-oss-120b   (OPENROUTER_API_KEY) the same
                                 open model, from whichever host is cheapest/up (OpenRouter fails
                                 over between them)
                                 google_genai:gemini-3.8-flash    (GOOGLE_API_KEY)
    VISION_MODEL         reading alert screenshots; must accept images
                         default groq:qwen/qwen3.8-27b
    CURATOR_MODEL        the memory curator (memory/curator.py), which runs after each reply
                         default: LLM_MODEL. A smaller, faster model is fine: it fills in a
                         small structured form, and code checks everything it proposes
    TRANSCRIPTION_MODEL  voice notes (see tools/transcribe.py)
                         default groq:whisper-large-v3  — Groq's speech-to-text API
                         or a chat model that accepts audio, e.g. google_genai:gemini-3.8-flash

LLM_REQUESTS_PER_MINUTE (optional) paces all chat-model calls from this process to stay
under a provider quota. Calls wait rather than fail.
"""
import os
from functools import lru_cache

import httpx
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


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Idle connections are dropped after this many seconds instead of being reused. On Render, the
# first message after a quiet spell took ~40 s: a pooled connection had died while idle, the
# request waited out the whole timeout, then the retry worked. A fresh connection costs a TLS
# handshake (a fraction of a second).
KEEPALIVE_SECONDS = float(os.getenv("LLM_KEEPALIVE_SECONDS", "20"))


def _http_client(timeout: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SECONDS))


def _openrouter_providers() -> dict:
    order = [p.strip() for p in os.getenv("OPENROUTER_PROVIDERS", "groq,cerebras").split(",") if p.strip()]
    if order:
        return {"order": order, "allow_fallbacks": True}
    return {"sort": os.getenv("OPENROUTER_PROVIDER_SORT", "latency")}


def _build(model: str, timeout: int) -> BaseChatModel:
    if model.startswith("google_genai:") and not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    if model.startswith("openrouter:"):
        # OpenRouter speaks the OpenAI API: same client, its own address and key
        # provider.order: try these hosts first (Groq, where the agents were validated, then
        # Cerebras: both answer in about a second), falling back to others only if they're down.
        # Some other hosts stalled for over a minute. OPENROUTER_PROVIDERS="" lets OpenRouter pick
        # by provider.sort instead.
        # reasoning.effort: gpt-oss is a reasoning model and some hosts default to "high", which
        # can think for minutes (one call took 12); "medium" matches Groq, where it was validated.
        # OPENROUTER_PROVIDER_SORT: latency (default) / throughput / price.
        # OPENROUTER_REASONING_EFFORT: low / medium (default) / high.
        return init_chat_model("openai:" + model.removeprefix("openrouter:"), temperature=0, timeout=timeout,
                               max_retries=2, rate_limiter=_rate_limiter(),
                               base_url=os.getenv("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
                               api_key=os.getenv("OPENROUTER_API_KEY"),
                               http_async_client=_http_client(timeout),
                               extra_body={"provider": _openrouter_providers(),
                                           "reasoning": {"effort": os.getenv("OPENROUTER_REASONING_EFFORT", "medium")}})
    return init_chat_model(model, temperature=0, timeout=timeout, max_retries=2, rate_limiter=_rate_limiter())


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    # a normal call takes 1-3 s: a stalled one is cut off and retried well before the trader gives up
    return _build(os.getenv("LLM_MODEL", DEFAULT_MODEL), timeout=20)


@lru_cache(maxsize=1)
def get_curator_model() -> BaseChatModel:
    return _build(os.getenv("CURATOR_MODEL") or os.getenv("LLM_MODEL", DEFAULT_MODEL), timeout=30)


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

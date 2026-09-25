"""The one place that decides which chat model the bookkeeper uses.

Switch providers with env vars only, no code changes:
    LLM_MODEL=google_genai:gemini-3.8-flash      (default; needs GOOGLE_API_KEY or GEMINI_API_KEY)
    LLM_MODEL=anthropic:claude-sonnet-5          (needs ANTHROPIC_API_KEY + `uv add langchain-anthropic`)

Voice notes and alert screenshots use TRANSCRIPTION_MODEL (default: the same Gemini
model), which must accept audio and images.

LLM_REQUESTS_PER_MINUTE (optional) paces all model calls from this process to stay under
a provider quota — e.g. 4 on Gemini's free tier (5/min). Calls wait rather than fail.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

load_dotenv()

DEFAULT_MODEL = "google_genai:gemini-3.8-flash"


@lru_cache(maxsize=1)
def _rate_limiter() -> InMemoryRateLimiter | None:
    rpm = os.getenv("LLM_REQUESTS_PER_MINUTE")
    if not rpm:
        return None
    # one shared bucket, so chat and transcription together stay under the limit
    return InMemoryRateLimiter(requests_per_second=float(rpm) / 60, check_every_n_seconds=0.2, max_bucket_size=1)


def _build(env_var: str, timeout: int) -> BaseChatModel:
    model = os.getenv(env_var, DEFAULT_MODEL)
    if model.startswith("google_genai:") and not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    return init_chat_model(model, temperature=0, timeout=timeout, max_retries=2, rate_limiter=_rate_limiter())


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    return _build("LLM_MODEL", timeout=30)


@lru_cache(maxsize=1)
def get_transcription_model() -> BaseChatModel:
    """Model that listens to voice notes and reads screenshots. Separate from the chat
    model because it must accept audio and images."""
    return _build("TRANSCRIPTION_MODEL", timeout=60)

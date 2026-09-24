"""The one place that decides which chat model the bookkeeper uses.

Switch providers with env vars only, no code changes:
    LLM_MODEL=google_genai:gemini-2.5-flash      (default; needs GOOGLE_API_KEY or GEMINI_API_KEY)
    LLM_MODEL=anthropic:claude-sonnet-5          (needs ANTHROPIC_API_KEY + `uv add langchain-anthropic`)

Voice notes use TRANSCRIPTION_MODEL (default: the same Gemini model), which must accept audio.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

load_dotenv()

DEFAULT_MODEL = "google_genai:gemini-2.5-flash"


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    if model.startswith("google_genai:") and not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    return init_chat_model(model, temperature=0, timeout=30, max_retries=2)


@lru_cache(maxsize=1)
def get_transcription_model() -> BaseChatModel:
    """Model that listens to voice notes. Separate from the chat model because it must
    accept audio: set TRANSCRIPTION_MODEL if LLM_MODEL points at a text-only model."""
    model = os.getenv("TRANSCRIPTION_MODEL", DEFAULT_MODEL)
    if model.startswith("google_genai:") and not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    return init_chat_model(model, temperature=0, timeout=60, max_retries=2)

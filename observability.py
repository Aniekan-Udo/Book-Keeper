"""Observability via Opik: one trace per trader message, with every model call, tool call
(arguments and result) and the final reply — so when the bot gets something wrong you can
see why.

Off unless configured. Set in .env:
    OPIK_API_KEY=...            (Comet-hosted Opik)   or   OPIK_URL_OVERRIDE=...  (self-hosted)
    OPIK_WORKSPACE=...
    OPIK_PROJECT_NAME=book-keeper   (optional)

Traces contain traders' messages and business figures, and go to Comet's servers (or
yours, if self-hosted) — the privacy notice must say so. Voice notes and screenshots are
turned into text before the agent runs, so raw audio and images are never sent.
"""
import os

DEFAULT_PROJECT = "book-keeper"


def opik_enabled() -> bool:
    return bool(os.getenv("OPIK_API_KEY") or os.getenv("OPIK_URL_OVERRIDE"))


def _drop_empty_settings() -> None:
    """docker compose passes unset variables as empty strings; Opik would read an empty
    OPIK_URL_OVERRIDE as its server address and fail every request."""
    for name in ("OPIK_URL_OVERRIDE", "OPIK_WORKSPACE"):
        if os.environ.get(name) == "":
            del os.environ[name]


def message_tracer(user_id: int, channel: str):
    """A tracer for one trader message, or None when Opik isn't configured. All of a
    trader's messages share a thread, so a conversation reads as one thread in Opik."""
    if not opik_enabled():
        return None
    _drop_empty_settings()
    from opik.integrations.langchain import OpikTracer  # only imported when used

    return OpikTracer(
        project_name=os.getenv("OPIK_PROJECT_NAME", DEFAULT_PROJECT),
        thread_id=f"user-{user_id}",
        tags=["book-keeper", channel],
        metadata={"user_id": user_id, "channel": channel},
    )


def trace_id_of(tracer) -> str | None:
    """The Opik trace id a tracer created (to store next to the conversation turn)."""
    if tracer is None:
        return None
    traces = tracer.created_traces()
    return traces[-1].id if traces else None


def flush() -> None:
    """Send any buffered traces now (for short-lived scripts; the server sends in the background)."""
    if opik_enabled():
        import opik
        opik.flush_tracker()

import hashlib
import re

import structlog
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import SESSION_COOKIE
from db import IdempotencyRecord


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()


def user_or_ip_key(request: Request) -> str:
    """Rate-limit per logged-in browser session, else per IP. Nigerian mobile networks put
    many subscribers behind one carrier IP (CGNAT), so IP-only limits would throttle
    unrelated users together. Only used where the session is checked before the limit
    counts (authenticated endpoints); pre-login endpoints key on IP explicitly, so a made-up
    cookie can't dodge their limits."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return "session:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    return get_remote_address(request)


limiter = Limiter(key_func=user_or_ip_key)


async def get_cached_response(db: AsyncSession, key: str, user_id: int) -> dict | None:
    if not key:
        return None
    result = await db.execute(select(IdempotencyRecord).where(IdempotencyRecord.key == key))
    record = result.scalar_one_or_none()
    if record and record.user_id == user_id:
        return record.response
    return None

async def store_response(db: AsyncSession, key: str, user_id: int, response: dict):
    if not key:
        return
    db.add(IdempotencyRecord(key=key, user_id=user_id, response=response))
    await db.commit()


_STATUS_IN_MESSAGE = re.compile(r"\b(402|429|503)\b")
# billing problems some providers report with other status codes
_BILLING_MARKERS = ("organization_delinquent", "overdue payment", "prepayment credits are depleted")


def provider_problem(exc: Exception) -> str | None:
    """Classify an LLM provider failure that isn't our bug, across providers, without
    importing each SDK's exception types. Checks the status code attributes and, since some
    SDKs (e.g. langchain-google-genai) wrap errors, the status in the message too.

    Returns "rate_limited" (429), "out_of_credit" (402: prepaid credit used up, or Groq's
    400 "organization_delinquent" for overdue payment), "overloaded" (503), or None."""
    if any(marker in str(exc) for marker in _BILLING_MARKERS):
        return "out_of_credit"
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        match = _STATUS_IN_MESSAGE.search(str(exc))
        status = int(match.group(1)) if match else None
    if status == 402:
        return "out_of_credit"
    if status == 429 or type(exc).__name__ in ("RateLimitError", "ResourceExhausted"):
        return "rate_limited"
    if status == 503:
        return "overloaded"
    return None

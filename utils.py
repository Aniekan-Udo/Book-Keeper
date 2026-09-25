import hashlib

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


def is_rate_limit_error(exc: Exception) -> bool:
    """Detects a 429/rate-limit error across providers by status code or exception class name,
    without needing to import every provider's SDK-specific exception type."""
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    exc_type_name = type(exc).__name__
    return status_code == 429 or exc_type_name in ("RateLimitError", "ResourceExhausted")

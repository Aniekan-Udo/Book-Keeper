import structlog
from datetime import datetime, date
from sqlalchemy import select
from db import IdempotencyRecord
from sqlalchemy.ext.asyncio import AsyncSession


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()

def format_naira(amount: float) -> str:
    return f"₦{amount:,.2f}"

def describe_period(start: date | None, end: date | None) -> str:
    if not start and not end:
        return "all time"
    if start == end:
        return f"on {start.strftime('%B %d, %Y')}"
    return f"from {start.strftime('%B %d, %Y')} to {end.strftime('%B %d, %Y')}"


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
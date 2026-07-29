# tools/formatting.py
from datetime import date

from db import get_db
from db import TransactionORM  # or wherever that class actually lives
from sqlalchemy.ext.asyncio import AsyncSession

async def resolve_clarification(db: AsyncSession, transaction_id: int, amount: float, user_id: int | None = None):
    txn = await db.get(TransactionORM, transaction_id)
    if txn is None:
        raise ValueError("Transaction not found.")
    if user_id is not None and txn.user_id != user_id:
        raise ValueError("Transaction not found for this user.")
    txn.amount = amount
    txn.status = "complete"
    await db.commit()
    await db.refresh(txn)
    return txn


def format_naira(amount: float) -> str:
    return f"₦{amount:,.2f}"

def describe_period(start: date | None, end: date | None) -> str:
    if not start and not end:
        return "all time"
    if start == end:
        return f"on {start.strftime('%B %d, %Y')}"
    return f"from {start.strftime('%B %d, %Y')} to {end.strftime('%B %d, %Y')}"
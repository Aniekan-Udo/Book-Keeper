from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import TransactionORM
from config import GenerateDate
from tools.formatting import format_naira, describe_period


async def _open_balances(db: AsyncSession, dates: GenerateDate, user_id: int, types: tuple[str, ...]) -> list[TransactionORM]:
    query = select(TransactionORM).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type.in_(types),
        TransactionORM.status == "complete",
        TransactionORM.is_deleted.is_(False),
        TransactionORM.outstanding > 0,
    )
    if dates.start_date:
        query = query.where(TransactionORM.date >= dates.start_date)
    if dates.end_date:
        query = query.where(TransactionORM.date <= dates.end_date)
    query = query.order_by(TransactionORM.date.desc())
    return list((await db.execute(query)).scalars().all())


def _entries(rows: list[TransactionORM]) -> list[dict]:
    return [
        {
            "id": r.id, "type": r.type, "party": r.party, "item": r.item,
            "amount": float(r.amount), "amount_paid": float(r.amount_paid),
            "outstanding": float(r.outstanding), "date": r.date,
        }
        for r in rows
    ]


def _by_party(rows: list[TransactionORM], unnamed: str) -> str:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    names: dict[str, str] = {}
    for r in rows:
        # group "Mama Chidi" and "mama chidi" together, display the first spelling seen
        key = (r.party or "").strip().lower() or unnamed
        names.setdefault(key, r.party.strip() if r.party else unnamed)
        totals[key] += r.outstanding
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{names[k]} {format_naira(v)}" for k, v in ranked)


async def get_recievables(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    """What other people owe the user: unpaid sales and money the user lent out."""
    rows = await _open_balances(db, dates, user_id, ("sale", "loan_given"))
    period_desc = describe_period(dates.start_date, dates.end_date)
    total = sum((r.outstanding for r in rows), Decimal("0"))

    if not rows:
        summary = f"Nobody owes you anything {period_desc}."
    else:
        summary = (
            f"{period_desc.capitalize()}, you're owed {format_naira(total)} in total: "
            f"{_by_party(rows, 'unnamed customer')}."
        )

    return {
        "receivables": _entries(rows),
        "total_outstanding": float(total),
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }


async def get_payables(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    """What the user owes: unpaid purchases and money the user borrowed."""
    rows = await _open_balances(db, dates, user_id, ("expense", "loan_taken"))
    period_desc = describe_period(dates.start_date, dates.end_date)
    total = sum((r.outstanding for r in rows), Decimal("0"))

    if not rows:
        summary = f"You don't owe anybody anything {period_desc}."
    else:
        summary = (
            f"{period_desc.capitalize()}, you owe {format_naira(total)} in total: "
            f"{_by_party(rows, 'unnamed supplier')}."
        )

    return {
        "payables": _entries(rows),
        "total_outstanding": float(total),
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }

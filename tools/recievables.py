from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, datetime
from db import TransactionORM
from collections import defaultdict
from pydantic import BaseModel, model_validator
from datetime import date
from config import GenerateDate
from tools.formatting import format_naira, describe_period


async def get_recievables(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    query = select(TransactionORM).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type == "sale",
        TransactionORM.status == "complete",
        TransactionORM.outstanding > 0,
    )
    if dates.start_date:
        query = query.where(TransactionORM.date >= dates.start_date)
    if dates.end_date:
        query = query.where(TransactionORM.date <= dates.end_date)
    query = query.order_by(TransactionORM.date.desc())

    rows = (await db.execute(query)).scalars().all()

    receivables = [
        {
            "id": r.id, "party": r.party, "item": r.item,
            "amount": float(r.amount), "amount_paid": float(r.amount_paid),
            "outstanding": float(r.outstanding), "date": r.date,
        }
        for r in rows
    ]

    period_desc = describe_period(dates.start_date, dates.end_date)
    total_outstanding = sum(r["outstanding"] for r in receivables)

    if not receivables:
        summary = f"You have no outstanding payments owed to you {period_desc}."
    else:
        by_party = defaultdict(float)
        for r in receivables:
            key = r["party"] or "an unnamed customer"
            by_party[key] += r["outstanding"]

        lines = ", ".join(f"{format_naira(amt)} from {party}" for party, amt in by_party.items())
        count = len(receivables)
        sale_word = "sale" if count == 1 else "sales"
        summary = (
            f"{period_desc.capitalize()}, you're owed {format_naira(total_outstanding)} "
            f"across {count} unpaid {sale_word}: {lines}."
        )

    return {
        "receivables": receivables,
        "total_outstanding": total_outstanding,
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }
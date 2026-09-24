from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal
from db import TransactionORM
from config import GenerateDate

from tools.formatting import format_naira, describe_period


from collections import defaultdict

async def get_expenses(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    query = select(TransactionORM).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type == "expense",
        TransactionORM.status == "complete",
        TransactionORM.is_deleted.is_(False),
    )
    if dates.start_date:
        query = query.where(TransactionORM.date >= dates.start_date)
    if dates.end_date:
        query = query.where(TransactionORM.date <= dates.end_date)
    query = query.order_by(TransactionORM.date.desc())

    rows = (await db.execute(query)).scalars().all()

    expenses = [
        {
            "id": r.id, "party": r.party, "item": r.item,
            "amount": float(r.amount), "note": r.note, "date": r.date,
        }
        for r in rows
    ]

    period_desc = describe_period(dates.start_date, dates.end_date)
    total = sum((r.amount for r in rows), Decimal("0"))

    if not expenses:
        summary = f"You have no recorded expenses {period_desc}."
    else:
        # group by item to highlight where the money went, biggest first
        by_item = defaultdict(Decimal)
        for r in rows:
            by_item[r.item or "unspecified"] += r.amount

        top_items = sorted(by_item.items(), key=lambda x: x[1], reverse=True)[:3]
        breakdown = ", ".join(f"{format_naira(amt)} on {item}" for item, amt in top_items)

        count = len(expenses)
        item_word = "expense" if count == 1 else "expenses"
        summary = (
            f"{period_desc.capitalize()}, you had {count} {item_word} totaling {format_naira(total)}. "
            f"Your biggest spend{'s were' if len(top_items) > 1 else ' was'}: {breakdown}."
        )

    return {
        "expenses": expenses,
        "total": float(total),
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }

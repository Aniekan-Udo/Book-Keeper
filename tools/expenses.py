from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, datetime
from db import TransactionORM
from config import GenerateDate

from tools.formatting import format_naira, describe_period


from collections import defaultdict

async def get_expenses(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    query = select(TransactionORM).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type == "expense",
        TransactionORM.status == "complete",
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
    total = sum(e["amount"] for e in expenses)

    if not expenses:
        summary = f"You have no recorded expenses {period_desc}."
    else:
        # group by item to highlight where the money went, biggest first
        by_item = defaultdict(float)
        for e in expenses:
            key = e["item"] or "unspecified"
            by_item[key] += e["amount"]

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
        "total": total,
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }

# async def get_expenses(db: AsyncSession, dates: GenerateDate) -> list[dict]:
#     query = select(TransactionORM).where(
#         TransactionORM.type == "expense",
#         TransactionORM.status == "complete",
#     )
#     if dates.start_date:
#         query = query.where(TransactionORM.date >= dates.start_date)
#     if dates.end_date:
#         query = query.where(TransactionORM.date <= dates.end_date)
#     query = query.order_by(TransactionORM.date.desc())
#
#     rows = (await db.execute(query)).scalars().all()
#
#     return [
#         {
#             "id": r.id, "party": r.party, "item": r.item,
#             "amount": float(r.amount), "note": r.note, "date": r.date,
#         }
#         for r in rows
#     ]
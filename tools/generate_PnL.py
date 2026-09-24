from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db import TransactionORM
from config import GenerateDate
from tools.formatting import format_naira, describe_period


async def generate_pnl(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    """Profit counts a sale when it's made, even if the customer hasn't paid yet
    (accrual) — the unpaid part is reported alongside so it isn't mistaken for cash."""
    def totals_for(txn_type: str):
        query = select(
            func.coalesce(func.sum(TransactionORM.amount), 0),
            func.coalesce(func.sum(TransactionORM.outstanding), 0),
        ).where(
            TransactionORM.user_id == user_id,
            TransactionORM.type == txn_type,
            TransactionORM.status == "complete",
            TransactionORM.is_deleted.is_(False),
        )
        if dates.start_date:
            query = query.where(TransactionORM.date >= dates.start_date)
        if dates.end_date:
            query = query.where(TransactionORM.date <= dates.end_date)
        return query

    revenue, unpaid_sales = (await db.execute(totals_for("sale"))).one()
    expenses, unpaid_expenses = (await db.execute(totals_for("expense"))).one()
    net_profit = revenue - expenses

    period_desc = describe_period(dates.start_date, dates.end_date)

    if revenue == 0 and expenses == 0:
        summary = f"You have no recorded sales or expenses {period_desc}."
    else:
        summary = (
            f"{period_desc.capitalize()}, you made {format_naira(revenue)} in sales "
            f"and spent {format_naira(expenses)}, "
        )
        if net_profit > 0:
            summary += f"for a net profit of {format_naira(net_profit)}."
        elif net_profit < 0:
            summary += f"resulting in a net loss of {format_naira(abs(net_profit))}."
        else:
            summary += "so you broke even."
        if unpaid_sales > 0:
            summary += f" {format_naira(unpaid_sales)} of those sales is still owed to you."
        if unpaid_expenses > 0:
            summary += f" You still owe suppliers {format_naira(unpaid_expenses)} from these purchases."

    return {
        "revenue": float(revenue),
        "expenses": float(expenses),
        "net_profit": float(net_profit),
        "unpaid_sales": float(unpaid_sales),
        "unpaid_expenses": float(unpaid_expenses),
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }

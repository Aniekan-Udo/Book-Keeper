from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, datetime
from db import TransactionORM

from pydantic import BaseModel, model_validator
from datetime import date
from config import GenerateDate
from tools.formatting import format_naira, describe_period


async def generate_pnl(db: AsyncSession, dates: GenerateDate, user_id: int) -> dict:
    revenue_query = select(func.coalesce(func.sum(TransactionORM.amount_paid), 0)).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type == "sale",
        TransactionORM.status == "complete",
    )
    expense_query = select(func.coalesce(func.sum(TransactionORM.amount), 0)).where(
        TransactionORM.user_id == user_id,
        TransactionORM.type == "expense",
        TransactionORM.status == "complete",
    )

    if dates.start_date:
        revenue_query = revenue_query.where(TransactionORM.date >= dates.start_date)
        expense_query = expense_query.where(TransactionORM.date >= dates.start_date)
    if dates.end_date:
        revenue_query = revenue_query.where(TransactionORM.date <= dates.end_date)
        expense_query = expense_query.where(TransactionORM.date <= dates.end_date)

    revenue = float((await db.execute(revenue_query)).scalar())
    expenses = float((await db.execute(expense_query)).scalar())
    net_profit = revenue - expenses

    period_desc = describe_period(dates.start_date, dates.end_date)

    if revenue == 0 and expenses == 0:
        summary = f"You have no recorded sales or expenses {period_desc}."
    elif net_profit > 0:
        summary = (
            f"{period_desc.capitalize()}, you made {format_naira(revenue)} in sales "
            f"and spent {format_naira(expenses)}, for a net profit of {format_naira(net_profit)}."
        )
    elif net_profit < 0:
        summary = (
            f"{period_desc.capitalize()}, you made {format_naira(revenue)} in sales "
            f"and spent {format_naira(expenses)}, resulting in a net loss of {format_naira(abs(net_profit))}."
        )
    else:
        summary = (
            f"{period_desc.capitalize()}, you made {format_naira(revenue)} in sales "
            f"and spent exactly the same in expenses — you broke even."
        )

    return {
        "revenue": revenue,
        "expenses": expenses,
        "net_profit": net_profit,
        "period": {"start": dates.start_date, "end": dates.end_date},
        "summary": summary,
    }
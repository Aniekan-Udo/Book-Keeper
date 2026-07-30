from sqlalchemy import select
from langchain_core.tools import tool
from pydantic import BaseModel, model_validator
from typing import Optional, Literal
from datetime import date as date_type
from sqlalchemy.ext.asyncio import AsyncSession
from db import TransactionORM
from config import GenerateDate
from tools.generate_PnL import generate_pnl
from tools.expenses import get_expenses
from tools.recievables import get_recievables as get_receivables
from tools.formatting import format_naira
from tools.prompts import router_sys_msg

from monitoring import tracer

class TransactionInput(BaseModel):
    type: Literal["sale", "expense", "loan_taken", "loan_given", "debt_repayment"]
    party: Optional[str] = None
    item: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    amount: Optional[float] = None
    amount_paid: Optional[float] = None
    direction: Literal["in", "out"]
    note: Optional[str] = None
    date: Optional[date_type] = None

    @model_validator(mode="after")
    def default_amount_paid(self):
        # if the model didn't distinguish partial payment, assume full payment
        if self.amount is not None and self.amount_paid is None:
            self.amount_paid = self.amount
        # treat a stated 0 the same as "not stated" — never silently accept 0 as valid
        if self.amount == 0:
            self.amount = None
        return self


# ── shared persistence logic, used by the tool below ──
async def persist_transactions(db: AsyncSession, transactions: list[TransactionInput], user_id: int) -> dict:
    db_rows = []
    for t in transactions:
        status = "complete" if t.amount is not None else "needs_amount"
        outstanding = None
        if t.amount is not None and t.amount_paid is not None:
            outstanding = t.amount - t.amount_paid
            if outstanding < 0:
                outstanding = 0  # overpayment shouldn't produce a negative receivable

        db_rows.append(TransactionORM(
            user_id=user_id,
            type=t.type, party=t.party, item=t.item, quantity=t.quantity,
            unit=t.unit, amount=t.amount, amount_paid=t.amount_paid,
            outstanding=outstanding, direction=t.direction,
            note=t.note,
            date=t.date or date_type.today(),
            status=status,
        ))

    try:
        db.add_all(db_rows)
        await db.flush()
    except Exception:
        await db.rollback()
        raise

    clarifications = [
        {"transaction_id": row.id, "message": f"You mentioned {t.item or t.type} — what was the amount?"}
        for t, row in zip(transactions, db_rows)
        if row.status == "needs_amount"
    ]

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {"transactions_logged": len(db_rows), "clarifications": clarifications}


class TransactionQuery(BaseModel):
    start_date: Optional[date_type] = None
    end_date: Optional[date_type] = None
    amount: Optional[float] = None
    item_or_party: Optional[str] = None  # free-text hint, e.g. "fuel", "Sarah"

# ── factory: builds tools bound to a specific db session ──
def build_tools(db: AsyncSession, user_id: int):

    @tool
    async def extract_transactions_tool(transactions: list[TransactionInput]) -> dict:
        """Use this when the user is describing something that happened: a sale, an
        expense, a loan, or a repayment. Extract every distinct transaction mentioned
        as a separate item in the list, following the amount/disambiguation/
        no-fabrication rules from your system instructions."""
        with tracer.start_as_current_span("extract_transactions") as span:
            span.set_attribute("transactions.count", len(transactions))
            span.set_attribute("user_id", user_id)
            result = await persist_transactions(db, transactions, user_id)
            span.set_attribute("transactions.persisted", result["transactions_logged"])
            return result


    @tool
    async def query_transactions_tool(query: TransactionQuery) -> dict:
        """Use this when the user asks about a specific transaction — what they spent
        money on, who they paid, or details about a particular past entry — rather than
        a summary report."""
        with tracer.start_as_current_span("query_transactions") as span:
            span.set_attribute("query.start_date", str(query.start_date))
            span.set_attribute("query.end_date", str(query.end_date))

            stmt = select(TransactionORM).where(TransactionORM.status == "complete",
                                                TransactionORM.user_id == user_id,)
            if query.start_date:
                stmt = stmt.where(TransactionORM.date >= query.start_date)
            if query.end_date:
                stmt = stmt.where(TransactionORM.date <= query.end_date)
            if query.amount:
                stmt = stmt.where(TransactionORM.amount == query.amount)

            rows = (await db.execute(stmt)).scalars().all()

            if not rows:
                span.set_attribute("query.matches", 0)
                return {"summary": "I couldn't find a matching transaction for that."}

            lines = []
            for r in rows:
                desc = r.item or r.party or "an unspecified item"
                note_part = f" ({r.note})" if r.note else ""
                lines.append(f"{format_naira(r.amount)} on {desc}{note_part} on {r.date.strftime('%B %d, %Y')}")

            summary = "You spent " + "; ".join(lines) + "."
            span.set_attribute("query.matches", len(rows))
            return {"summary": summary, "matches": len(rows)}


    @tool
    async def generate_report_tool(
            report_type: Literal["pnl", "expenses", "receivables"],
            start_date: Optional[date_type] = None,
            end_date: Optional[date_type] = None,
    ) -> dict:
        """Use this when the user asks for their PnL, expenses, or what customers still
        owe them (receivables). Convert relative time language into actual calendar
        dates. Leave both dates null for all-time."""
        with tracer.start_as_current_span("generate_report") as span:
            span.set_attribute("report.type", report_type)
            dates = GenerateDate(start_date=start_date, end_date=end_date)
            if report_type == "pnl":
                return await generate_pnl(db, dates, user_id)
            elif report_type == "expenses":
                return await get_expenses(db, dates, user_id)
            return await get_receivables(db, dates, user_id)

    return [extract_transactions_tool, generate_report_tool, query_transactions_tool]


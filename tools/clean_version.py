from datetime import date as date_type
from typing import Optional, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from config import GenerateDate
from db import TransactionORM
from monitoring import tracer
from tools.expenses import get_expenses
from tools.formatting import format_naira
from tools.generate_PnL import generate_pnl
from tools.ledger import (
    LedgerError, TransactionInput, correct_transaction, delete_transactions, persist_transactions,
)
from tools.recievables import get_recievables as get_receivables, get_payables


class TransactionQuery(BaseModel):
    start_date: Optional[date_type] = None
    end_date: Optional[date_type] = None
    amount: Optional[float] = None
    item_or_party: Optional[str] = Field(default=None, description='Free-text hint, e.g. "fuel", "Sarah"')
    type: Optional[Literal["sale", "expense", "loan_taken", "loan_given", "debt_repayment"]] = None


class Correction(BaseModel):
    transaction_id: Optional[int] = Field(
        default=None, description="The id to change. Leave null to change the entry from the user's last message.")
    amount: Optional[float] = None
    amount_paid: Optional[float] = None
    party: Optional[str] = None
    item: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    note: Optional[str] = None
    date: Optional[date_type] = None


_QUERY_LIMIT = 20


def _describe_for_user(r: TransactionORM) -> str:
    amount = format_naira(r.amount) if r.amount is not None else "an amount not yet given"
    what = r.item or "something"
    who = r.party
    day = r.date.strftime('%B %d, %Y')
    if r.type == "sale":
        line = f"sold {what}{f' to {who}' if who else ''} for {amount}"
    elif r.type == "expense":
        line = f"spent {amount} on {what}{f' from {who}' if who else ''}"
    elif r.type == "loan_taken":
        line = f"borrowed {amount}{f' from {who}' if who else ''}"
    elif r.type == "loan_given":
        line = f"lent {amount}{f' to {who}' if who else ''}"
    elif r.direction == "in":
        line = f"received a {amount} repayment{f' from {who}' if who else ''}"
    else:
        line = f"repaid {amount}{f' to {who}' if who else ''}"
    if r.outstanding:
        line += f" ({format_naira(r.outstanding)} still unpaid)"
    if r.note:
        line += f" — {r.note}"
    return f"#{r.id}: {line} on {day}"


# ── factory: builds tools bound to a specific db session and user ──
def build_tools(db: AsyncSession, user_id: int, clarifications: list | None = None):
    """`clarifications`, if given, collects amount prompts raised during this request so the
    endpoint can hand them to the client as structured data, not just reply text."""

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
            span.set_attribute("transactions.persisted", len(result["logged"]))
            if clarifications is not None:
                clarifications.extend(result["clarifications"])
            return result

    @tool
    async def query_transactions_tool(query: TransactionQuery) -> dict:
        """Use this when the user asks about specific past transactions — what they spent
        money on, who they paid, what they sold to someone — rather than a summary report.
        Also use it to find the id of an entry the user wants to change or remove."""
        with tracer.start_as_current_span("query_transactions") as span:
            span.set_attribute("query.start_date", str(query.start_date))
            span.set_attribute("query.end_date", str(query.end_date))

            stmt = select(TransactionORM).where(
                TransactionORM.user_id == user_id,
                TransactionORM.is_deleted.is_(False),
            )
            if query.start_date:
                stmt = stmt.where(TransactionORM.date >= query.start_date)
            if query.end_date:
                stmt = stmt.where(TransactionORM.date <= query.end_date)
            if query.amount:
                stmt = stmt.where(TransactionORM.amount == query.amount)
            if query.type:
                stmt = stmt.where(TransactionORM.type == query.type)
            if query.item_or_party:
                pattern = f"%{query.item_or_party.strip()}%"
                stmt = stmt.where(or_(
                    TransactionORM.item.ilike(pattern),
                    TransactionORM.party.ilike(pattern),
                    TransactionORM.note.ilike(pattern),
                ))
            stmt = stmt.order_by(TransactionORM.date.desc(), TransactionORM.id.desc()).limit(_QUERY_LIMIT + 1)

            rows = (await db.execute(stmt)).scalars().all()
            span.set_attribute("query.matches", len(rows))

            if not rows:
                return {"summary": "I couldn't find a matching transaction for that."}

            truncated = len(rows) > _QUERY_LIMIT
            lines = [_describe_for_user(r) for r in rows[:_QUERY_LIMIT]]
            summary = "You " + "; ".join(lines) + "."
            if truncated:
                summary += f" (Showing the {_QUERY_LIMIT} most recent — narrow the dates to see more.)"
            return {"summary": summary, "matches": len(lines)}

    @tool
    async def generate_report_tool(
            report_type: Literal["pnl", "expenses", "receivables", "payables"],
            start_date: Optional[date_type] = None,
            end_date: Optional[date_type] = None,
    ) -> dict:
        """Use this when the user asks for their profit/PnL, expenses, what people owe
        them (receivables) or what they owe others (payables). Convert relative time
        language into actual calendar dates. Leave both dates null for all-time."""
        with tracer.start_as_current_span("generate_report") as span:
            span.set_attribute("report.type", report_type)
            dates = GenerateDate(start_date=start_date, end_date=end_date)
            if report_type == "pnl":
                return await generate_pnl(db, dates, user_id)
            if report_type == "expenses":
                return await get_expenses(db, dates, user_id)
            if report_type == "payables":
                return await get_payables(db, dates, user_id)
            return await get_receivables(db, dates, user_id)

    @tool
    async def undo_transaction_tool(transaction_id: Optional[int] = None) -> dict:
        """Use this when the user wants to cancel, remove or undo an entry ("cancel that",
        "delete the fuel expense"). Leave transaction_id null to undo everything recorded
        from their last message; otherwise pass the id (find it with query_transactions_tool)."""
        with tracer.start_as_current_span("undo_transaction"):
            try:
                return await delete_transactions(db, user_id, transaction_id)
            except LedgerError as e:
                return {"error": str(e)}

    @tool
    async def correct_transaction_tool(correction: Correction) -> dict:
        """Use this when the user says an entry is wrong and gives the right value
        ("it was 45k not 50k", "that was Tunde not Tayo", "that happened yesterday").
        Only fill the fields that change. Leave transaction_id null for the entry from
        their last message; otherwise find the id with query_transactions_tool."""
        with tracer.start_as_current_span("correct_transaction"):
            fields = correction.model_dump(exclude={"transaction_id"}, exclude_none=True)
            try:
                return await correct_transaction(db, user_id, correction.transaction_id, **fields)
            except LedgerError as e:
                return {"error": str(e)}

    return [
        extract_transactions_tool, generate_report_tool, query_transactions_tool,
        undo_transaction_tool, correct_transaction_tool,
    ]

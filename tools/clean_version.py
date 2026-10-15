from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, timezone
from typing import Callable, Optional, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from config import GenerateDate, LooseDate
from db import TransactionORM, User
from tools import alerts, debt_collection, statements
from tools.contacts import save_contact
from tools.expenses import get_expenses
from tools.formatting import format_naira
from tools.generate_PnL import generate_pnl
from tools.ledger import (
    LedgerError, TransactionInput, correct_transaction, delete_transactions, persist_transactions,
)
from tools.recievables import get_recievables as get_receivables, get_payables
from memory import get_graph_memory
from memory.collector import Collector
from memory.explain import explain, resolve_party
from memory.models import GraphMemoryError


class TransactionQuery(BaseModel):
    start_date: LooseDate = None
    end_date: LooseDate = None
    amount: Optional[float] = None
    item_or_party: Optional[str] = Field(default=None, description='Free-text hint, e.g. "fuel", "Sarah"')
    type: Optional[Literal["sale", "expense", "loan_taken", "loan_given", "debt_repayment"]] = None


class Correction(BaseModel):
    transaction_id: Optional[int] = Field(
        default=None, description="Null = the entry from the user's last message")
    amount: Optional[float] = None
    amount_paid: Optional[float] = None
    party: Optional[str] = None
    item: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    note: Optional[str] = None
    date: LooseDate = None
    due_date: LooseDate = None


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
def build_tools(db: AsyncSession, user_id: int, clarifications: list | None = None,
                request_started_at: datetime | None = None, touched: list | None = None,
                could_be: Callable[[str], list[str]] | None = None, last_reply: str = "",
                memory_notes: list | None = None):
    """`clarifications`, if given, collects amount prompts raised during this request so the
    endpoint can hand them to the client as structured data, not just reply text.
    `request_started_at` stops a reminder drafted in this request from also being sent in it.
    `touched`, if given, collects the entries recorded or corrected in this request (as
    ledger.describe dicts), for the memory curator.
    `could_be(name)`, from graph memory (memory.recall.Recalled.could_be), returns the known
    people a new name might really be. Recording under such a name is refused until the user
    has said who they mean, so one customer's debts don't split across two names.
    `last_reply` is the assistant's previous message: a name only counts as confirmed new
    if that message asked about it (the model can't wave its own guess through).
    `memory_notes`, if given, collects what tools want remembered (a confirmed bank name, a
    sent reminder); the curator writes them once the message is saved."""
    started = request_started_at or datetime.now(timezone.utc)
    # people a refused name might be: blocked for the rest of this request, so the agent
    # asks the user instead of picking one of them itself
    unresolved: dict[str, str] = {}
    refused: set[str] = set()
    asked = " ".join(last_reply.lower().split())

    @asynccontextmanager
    async def this_session():
        yield db  # the request's session; tools run one at a time

    def collector() -> Collector:
        return Collector(get_graph_memory(), this_session)

    @tool
    async def extract_transactions_tool(transactions: list[TransactionInput],
                                        confirmed_new_names: Optional[list[str]] = None) -> dict:
        """Record what happened: sales, expenses, loans, repayments — one item per transaction mentioned. confirmed_new_names: names the user confirmed are new people, not someone already known."""
        if could_be:
            # confirmed only if the previous message asked about the name, and not a name
            # refused earlier in this same request (the user hasn't answered yet)
            confirmed = {k for k in (" ".join(n.lower().split()) for n in (confirmed_new_names or []))
                         if k in asked and k not in refused}
            for t in transactions:
                key = " ".join(t.party.lower().split()) if t.party else ""
                if not key or key in confirmed:
                    continue
                candidates = could_be(t.party)
                if candidates:
                    refused.add(key)
                    for c in candidates:
                        unresolved[c.lower()] = t.party
                    return {"error": (f'Nothing recorded yet: "{t.party}" could be {" or ".join(candidates)}. '
                                      "Ask the user who they mean (or if it's someone new). Record nothing until "
                                      "they answer.")}
                if key in unresolved:
                    return {"error": (f'Nothing recorded yet: the user hasn\'t said whether "{unresolved[key]}" '
                                      f"is {t.party}. Ask them first.")}
        result = await persist_transactions(db, transactions, user_id)
        if clarifications is not None:
            clarifications.extend(result["clarifications"])
        if touched is not None:
            touched.extend(result["logged"])
        return result

    @tool
    async def query_transactions_tool(query: TransactionQuery) -> dict:
        """Look up specific past entries ("what did I sell to Tunde?", "wetin I spend on fuel?"), or find an entry's id."""

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
            start_date: LooseDate = None,
            end_date: LooseDate = None,
    ) -> dict:
        """Profit (pnl), expenses, receivables (who owes the user) or payables (who the user owes), for a period."""
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
        """Cancel/undo entries ("cancel that", "delete am"). No id = everything from the user's last message."""
        try:
            return await delete_transactions(db, user_id, transaction_id)
        except LedgerError as e:
            return {"error": str(e)}

    @tool
    async def correct_transaction_tool(correction: Correction) -> dict:
        """Fix an entry ("it was 45k not 50k", "na Tunde, no be Tayo", add a due date). Fill only fields that change; no id = the entry from the last message."""
        fields = correction.model_dump(exclude={"transaction_id"}, exclude_none=True)
        try:
            result = await correct_transaction(db, user_id, correction.transaction_id, **fields)
        except LedgerError as e:
            return {"error": str(e)}
        if touched is not None:
            touched.append(result["updated"])
        return result

    @tool
    async def save_contact_tool(name: str, phone: str) -> dict:
        """Save a customer's/supplier's phone number ("Mama Chidi number na 0803…"). Use the name as used for their debts."""
        try:
            contact = await save_contact(db, user_id, name, phone)
            result = {"saved": contact.name, "phone": contact.phone}  # read before commit expires it
            await db.commit()
            return result
        except ValueError as e:
            await db.rollback()
            return {"error": str(e)}

    @tool
    async def remind_debtor_tool(party: str, trader_confirmed: bool = False) -> dict:
        """Draft a payment reminder to someone who owes the user ("remind Mama Chidi"). Only drafts; returns the message to show. trader_confirmed: only after the user, asked about a do-not-remind, clearly said to go ahead."""
        key = " ".join(party.lower().split())
        try:
            found = await resolve_party(get_graph_memory(), user_id, party)
            key = found[0] if found else key
        except GraphMemoryError:
            pass
        reason = await collector().do_not_remind(user_id, key)
        if reason and not (trader_confirmed and key in asked and "remind" in asked):
            return {"needs_confirmation": f"Not drafted: {reason}. Ask the user if they still want to remind "
                                          f"{party}. Only if they clearly say yes, call again with "
                                          "trader_confirmed=true."}
        user = await db.get(User, user_id)
        try:
            return await debt_collection.draft_reminder(db, user, party)
        except debt_collection.ReminderRefused as e:
            return {"error": str(e)}

    @tool
    async def confirm_reminder_tool(reminder_id: int) -> dict:
        """Send a reminder draft — only after the user saw it and said yes in a later message."""
        user = await db.get(User, user_id)
        try:
            result = await debt_collection.confirm_reminder(db, user, reminder_id, not_before=started)
        except debt_collection.ReminderRefused as e:
            return {"error": str(e)}
        if memory_notes is not None:
            memory_notes.append({"kind": "reminder_sent", "reminder_id": result["reminder_id"],
                                 "party": result["party"]})
        return result

    @tool
    async def who_to_chase_tool() -> dict:
        """Who to chase for payment first, with the reasons ("who I suppose chase?", "who never pay me?"). Also says who was skipped and why."""
        return await collector().who_to_chase(user_id)

    @tool
    async def explain_tool(person: str) -> dict:
        """Why the assistant thinks or did something about a person: their promises and outcomes, what's remembered about them and from which of the user's messages ("why Emeka first?", "wetin you know about Bola?")."""
        return await explain(get_graph_memory(), this_session, user_id, person)

    @tool
    async def customer_statement_tool(
            party: str,
            start_date: LooseDate = None,
            end_date: LooseDate = None,
            share_link: bool = True,
    ) -> dict:
        """Statement of everything with one person ("send me Mama Chidi statement"). Returns short text and a private link the user can forward to that person."""
        user = await db.get(User, user_id)
        try:
            st = await statements.build_statement(db, user, party, start_date, end_date)
        except statements.StatementError as e:
            return {"error": str(e)}
        result = {"statement": statements.as_text(st), "balance": st.balance_sentence()}
        if share_link:
            url = statements.link_url(await statements.share_link(db, user, st.party))
            if url:
                result["link"] = url
                result["link_note"] = ("Anyone with this link can view this statement for 30 days. "
                                       "It shows only dealings with this person.")
        return result

    @tool
    async def stop_sharing_statement_tool(party: str) -> dict:
        """Withdraw statement links shared for a person."""
        count = await statements.revoke_links(db, user_id, party)
        return {"links_withdrawn": count}

    @tool
    async def review_bank_alert_tool(alert_text: str) -> dict:
        """A forwarded bank/POS alert or "(forwarded alert screenshot)": pass its text unchanged. Records a clear repayment itself, or returns a question to ask."""
        return await alerts.review_alert(db, user_id, alert_text)

    @tool
    async def resolve_bank_alert_tool(
            alert_id: int,
            record_as: alerts.RecordAs,
            party: Optional[str] = None,
            item: Optional[str] = None,
            transaction_id: Optional[int] = None,
    ) -> dict:
        """Record the user's answer about a bank alert. record_as: sale (item, party), debt_repayment (party), expense (item), loan_taken/loan_given (party), already_recorded (transaction_id), ignore (personal)."""
        try:
            result = await alerts.resolve_alert(db, user_id, alert_id, record_as, party, item, transaction_id)
        except alerts.AlertError as e:
            return {"error": str(e)}
        if memory_notes is not None and result.get("bank_name") and party:
            memory_notes.append({"kind": "bank_alias", "alias": result["bank_name"], "party": party})
        return result

    return [
        extract_transactions_tool, generate_report_tool, query_transactions_tool,
        undo_transaction_tool, correct_transaction_tool,
        save_contact_tool, remind_debtor_tool, confirm_reminder_tool,
        customer_statement_tool, stop_sharing_statement_tool,
        review_bank_alert_tool, resolve_bank_alert_tool,
        who_to_chase_tool, explain_tool,
    ]

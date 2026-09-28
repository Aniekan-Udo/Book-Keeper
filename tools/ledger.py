"""Core bookkeeping rules: recording entries, settling debts, undo and corrections.

Nothing in here knows about the LLM — the agent tools in tools/clean_version.py call
these functions, and so do the HTTP endpoints. Keep the money rules in one place.
"""
import uuid
from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import LooseDate, business_today
from db import TransactionORM, RepaymentAllocation
from tools.contacts import save_contact
from tools.formatting import format_naira

ZERO = Decimal("0.00")

# which open entries a repayment can settle, keyed by the repayment's direction:
# money coming in settles what others owe the user; money going out settles what the user owes
SETTLEABLE_BY_DIRECTION = {
    "in": ("sale", "loan_given"),
    "out": ("expense", "loan_taken"),
}


class LedgerError(ValueError):
    """A user-facing problem (not found, ambiguous target) — safe to show in a reply."""


class TransactionInput(BaseModel):
    type: Literal["sale", "expense", "loan_taken", "loan_given", "debt_repayment"]
    party: Optional[str] = Field(default=None, description="Customer, supplier, lender or borrower, exactly as the user named them")
    item: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    amount: Optional[float] = Field(default=None, description="Full agreed amount / loan amount / repayment amount. Null if not stated.")
    amount_paid: Optional[float] = Field(
        default=None,
        description="Only for part-payment: how much of a sale or purchase was paid at the time, "
                    "or how much of a loan has already been repaid. Leave null otherwise.",
    )
    direction: Literal["in", "out"]
    note: Optional[str] = None
    date: LooseDate = None
    due_date: LooseDate = Field(
        default=None, description="When the other party promised to pay what's still owed, if they said")
    party_phone: Optional[str] = Field(
        default=None, description="The party's phone number, only if the user gave it")

    @model_validator(mode="after")
    def zero_means_unknown(self):
        # treat a stated 0 the same as "not stated" — never silently accept 0 as valid
        if self.amount == 0:
            self.amount = None
        return self


def to_money(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _party_key(party: str | None) -> str | None:
    return party.strip().lower() if party and party.strip() else None


def _settle(row: TransactionORM) -> None:
    """Derive status / amount_paid / outstanding from amount. amount_paid must not include
    repayment allocations when this runs (callers reverse allocations first)."""
    if row.amount is None:
        row.status = "needs_amount"
        row.outstanding = None
        return

    row.status = "complete"
    if row.type == "debt_repayment":
        row.amount_paid = None
        row.outstanding = None
        return

    if row.amount_paid is None:
        # a sale or purchase with no part-payment mentioned was paid in full;
        # a loan with no repayment mentioned hasn't been repaid yet
        row.amount_paid = row.amount if row.type in ("sale", "expense") else ZERO
    row.outstanding = max(row.amount - row.amount_paid, ZERO)


def describe(row: TransactionORM) -> dict:
    return {
        "id": row.id,
        "type": row.type,
        "party": row.party,
        "item": row.item,
        "amount": float(row.amount) if row.amount is not None else None,
        "amount_paid": float(row.amount_paid) if row.amount_paid is not None else None,
        "outstanding": float(row.outstanding) if row.outstanding is not None else None,
        "date": row.date.isoformat(),
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "status": row.status,
    }


def clarification_for(row: TransactionORM) -> dict:
    return {"transaction_id": row.id, "message": f"You mentioned {row.item or row.type.replace('_', ' ')} — what was the amount?"}


# ── repayments ───────────────────────────────────────────────────────────────

async def _apply_repayment(db: AsyncSession, repayment: TransactionORM) -> list[str]:
    """Settle the party's oldest open debts first (FIFO). Returns notes for the reply."""
    party = _party_key(repayment.party)
    amount_text = format_naira(repayment.amount)
    if party is None:
        return [f"I recorded the {amount_text} repayment, but I couldn't tell whose debt it settles. Who was it?"]

    open_debts = (await db.execute(
        select(TransactionORM)
        .where(
            TransactionORM.user_id == repayment.user_id,
            TransactionORM.is_deleted.is_(False),
            TransactionORM.status == "complete",
            TransactionORM.type.in_(SETTLEABLE_BY_DIRECTION[repayment.direction]),
            TransactionORM.outstanding > 0,
            func.lower(func.trim(TransactionORM.party)) == party,
        )
        .order_by(TransactionORM.date, TransactionORM.id)
        .with_for_update()
    )).scalars().all()

    if not open_debts:
        return [f"I recorded the {amount_text} repayment, but I have no open debt on record for {repayment.party}."]

    remaining = repayment.amount
    for debt in open_debts:
        if remaining <= 0:
            break
        applied = min(remaining, debt.outstanding)
        debt.outstanding -= applied
        debt.amount_paid = (debt.amount_paid or ZERO) + applied
        db.add(RepaymentAllocation(repayment_id=repayment.id, applied_to_id=debt.id, amount=applied))
        remaining -= applied

    await db.flush()
    notes = []
    if remaining > 0:
        notes.append(f"{format_naira(remaining)} of that is more than the recorded debt with {repayment.party}.")
    still_open = sum((d.outstanding for d in open_debts), ZERO)
    if still_open > 0:
        notes.append(f"{repayment.party} still has {format_naira(still_open)} outstanding.")
    else:
        notes.append(f"{repayment.party}'s debt is now fully settled.")
    return notes


async def _reverse_repayment(db: AsyncSession, repayment_id: int) -> None:
    allocations = (await db.execute(
        select(RepaymentAllocation).where(RepaymentAllocation.repayment_id == repayment_id)
    )).scalars().all()
    for alloc in allocations:
        debt = await db.get(TransactionORM, alloc.applied_to_id, with_for_update=True)
        debt.outstanding = (debt.outstanding or ZERO) + alloc.amount
        debt.amount_paid = (debt.amount_paid or ZERO) - alloc.amount
        await db.delete(alloc)
    await db.flush()


async def _repayments_applied_to(db: AsyncSession, debt_id: int) -> list[TransactionORM]:
    ids = (await db.execute(
        select(RepaymentAllocation.repayment_id).where(RepaymentAllocation.applied_to_id == debt_id).distinct()
    )).scalars().all()
    return [await db.get(TransactionORM, i) for i in ids]


# ── public operations ────────────────────────────────────────────────────────

async def persist_transactions(db: AsyncSession, transactions: list[TransactionInput], user_id: int) -> dict:
    batch_id = str(uuid.uuid4())
    try:
        rows = []
        for t in transactions:
            row = TransactionORM(
                user_id=user_id, batch_id=batch_id,
                type=t.type, party=t.party, item=t.item, quantity=t.quantity, unit=t.unit,
                amount=to_money(t.amount), amount_paid=to_money(t.amount_paid),
                direction=t.direction, note=t.note,
                date=t.date or business_today(), due_date=t.due_date,
                is_deleted=False,
            )
            _settle(row)
            rows.append(row)

        db.add_all(rows)
        await db.flush()

        notes = []
        for row in rows:
            if row.type == "debt_repayment" and row.status == "complete":
                notes.extend(await _apply_repayment(db, row))

        for t in transactions:
            if t.party and t.party_phone:
                try:
                    await save_contact(db, user_id, t.party, t.party_phone)
                    notes.append(f"Saved {t.party}'s number.")
                except ValueError:
                    notes.append(f"{t.party_phone} doesn't look like a Nigerian phone number, so I didn't save it.")

        # build the response before commit: committed rows are expired and can't lazy-load in async
        result = {
            "batch_id": batch_id,
            "logged": [describe(r) for r in rows],
            "clarifications": [clarification_for(r) for r in rows if r.status == "needs_amount"],
            "notes": notes,
        }
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return result


async def _latest_batch(db: AsyncSession, user_id: int) -> list[TransactionORM]:
    latest = (await db.execute(
        select(TransactionORM)
        .where(TransactionORM.user_id == user_id, TransactionORM.is_deleted.is_(False))
        .order_by(TransactionORM.created_at.desc(), TransactionORM.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if latest is None:
        raise LedgerError("There's nothing recorded yet.")
    if latest.batch_id is None:
        return [latest]
    return list((await db.execute(
        select(TransactionORM)
        .where(TransactionORM.user_id == user_id, TransactionORM.batch_id == latest.batch_id,
               TransactionORM.is_deleted.is_(False))
        .order_by(TransactionORM.id)
    )).scalars().all())


async def _get_owned(db: AsyncSession, user_id: int, transaction_id: int) -> TransactionORM:
    row = await db.get(TransactionORM, transaction_id)
    # same message for "doesn't exist" and "not yours", so ids can't be probed
    if row is None or row.user_id != user_id or row.is_deleted:
        raise LedgerError("I couldn't find that transaction.")
    return row


async def delete_transactions(db: AsyncSession, user_id: int, transaction_id: int | None = None) -> dict:
    """Soft-delete one transaction, or everything logged by the most recent message."""
    try:
        rows = [await _get_owned(db, user_id, transaction_id)] if transaction_id else await _latest_batch(db, user_id)

        affected_repayments: dict[int, TransactionORM] = {}
        for row in rows:
            row.is_deleted = True
            if row.type == "debt_repayment":
                await _reverse_repayment(db, row.id)
            else:
                for rep in await _repayments_applied_to(db, row.id):
                    affected_repayments[rep.id] = rep
        await db.flush()

        # money that was settling a now-deleted debt goes back to the party's other open debts
        for rep in affected_repayments.values():
            if not rep.is_deleted:
                await _reverse_repayment(db, rep.id)
                await _apply_repayment(db, rep)

        result = {"deleted": [describe(r) for r in rows]}
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return result


_CORRECTABLE = ("amount", "amount_paid", "party", "item", "quantity", "unit", "note", "date", "due_date")


async def correct_transaction(db: AsyncSession, user_id: int, transaction_id: int | None = None, **changes) -> dict:
    """Change fields on one transaction and keep every dependent balance consistent."""
    changes = {k: v for k, v in changes.items() if k in _CORRECTABLE and v is not None}
    if not changes:
        raise LedgerError("Tell me what to change — the amount, the person, the item or the date.")

    try:
        if transaction_id:
            row = await _get_owned(db, user_id, transaction_id)
        else:
            batch = await _latest_batch(db, user_id)
            if len(batch) > 1:
                options = "; ".join(f"#{r.id} {r.type} {r.item or r.party or ''}".strip() for r in batch)
                raise LedgerError(f"Your last message recorded several entries ({options}). Which one should I change?")
            row = batch[0]

        # take all repayment effects off this row, edit it, then put them back
        affected = [] if row.type == "debt_repayment" else await _repayments_applied_to(db, row.id)
        for rep in affected:
            await _reverse_repayment(db, rep.id)
        if row.type == "debt_repayment":
            await _reverse_repayment(db, row.id)

        was_paid_in_full = row.amount is not None and row.amount_paid == row.amount
        for field in ("amount", "amount_paid"):
            if field in changes:
                changes[field] = to_money(changes[field])
        for field, value in changes.items():
            setattr(row, field, value)
        if "amount" in changes and "amount_paid" not in changes and was_paid_in_full:
            row.amount_paid = row.amount

        _settle(row)
        await db.flush()

        notes = []
        if row.type == "debt_repayment" and row.status == "complete":
            notes.extend(await _apply_repayment(db, row))
        for rep in affected:
            await _apply_repayment(db, rep)

        result = {"updated": describe(row), "notes": notes}
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return result


async def resolve_clarification(db: AsyncSession, user_id: int, transaction_id: int, amount: float) -> dict:
    return await correct_transaction(db, user_id, transaction_id, amount=amount)

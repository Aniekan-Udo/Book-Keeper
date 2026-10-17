"""Bank and POS alerts the trader forwards: parse them, then decide what (if anything)
to record.

Read-only by design: we only read what the user chooses to forward. We never connect to
a bank, ask for logins, or act on an account (the app never touches money).

The model only extracts fields from the alert. What to record is decided here, in code:
- a clear repayment from the one person who owes that much → recorded straight away
  (undoable), because that's the typing we're saving the trader;
- anything uncertain — no match, several matches, more than they owe, possibly already
  recorded, money out, or a sender's "transfer successful" receipt — becomes a question.
"""
import base64
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import llm
from config import LooseDate, business_today
from db import BankAlert, Party, PartyAlias, TransactionORM
from tools.contacts import name_key
from tools.formatting import format_naira
from tools.ledger import SETTLEABLE_BY_DIRECTION, TransactionInput, persist_transactions, to_money
from tools.parties import party_id_for

DUPLICATE_WINDOW = timedelta(days=1)
SUPPORTED_IMAGES = {"image/jpeg", "image/png", "image/webp"}

PARSE_INSTRUCTIONS = """You read bank and POS alerts that a Nigerian trader forwarded to their bookkeeping assistant.
Alerts come from any Nigerian bank or fintech (GTBank, Access, Zenith, First Bank, UBA, Opay, Moniepoint, Palmpay,
Kuda, etc.) as SMS, email or app-notification text, and every provider formats them differently.

Extract only what the text says:
- kind: "credit_alert" = money came INTO the trader's account; "debit_alert" = money went OUT of it;
  "pos_receipt" = a POS terminal receipt; "sender_receipt" = someone else's "transfer successful" / "you sent"
  confirmation screen, which does NOT prove the money reached the trader; "other" = not an alert.
- direction: "in" if money came to the trader (credit alerts, the trader's own POS merchant receipts, and what a
  sender's receipt claims), "out" if the trader paid.
- amount in naira as a number (NGN10,000.00 → 10000). Ignore the account balance — never use it as the amount.
- counterparty: the other person/business name exactly as shown (e.g. "MAMA CHIDI OKAFOR", "SHOPRITE IKEJA"),
  often inside the description/narration ("TRF FROM ...", "NIP/...", "POS PURCHASE ..."). Null if not shown.
- narration: the description line, shortened. occurred_on: the date as YYYY-MM-DD. reference: session/transaction reference if shown.
- contains_secret: true if the text contains an OTP, PIN, password, full card number, or BVN.
If it isn't a bank/POS alert at all, set is_alert false."""

IMAGE_INSTRUCTIONS = """This is a screenshot or photo a Nigerian trader sent to their bookkeeping assistant.
If it shows a bank alert, bank-app transaction notification, transfer receipt or POS receipt, transcribe the text
that matters (amount, credit/debit, names, description, date, reference, and whether it is a "transfer successful"
screen from the sender's side). Otherwise set is_alert false. Never transcribe account balances, PINs, OTPs or
full card numbers."""


class ParsedAlert(BaseModel):
    is_alert: bool
    kind: Literal["credit_alert", "debit_alert", "pos_receipt", "sender_receipt", "other"]
    direction: Literal["in", "out"] | None = None
    amount: float | None = None
    counterparty: str | None = None
    narration: str | None = None
    occurred_on: LooseDate = None
    reference: str | None = None
    contains_secret: bool = False


class ImageReading(BaseModel):
    is_alert: bool
    text: str = Field(default="", description="The alert's text, transcribed")


# ── reading ──────────────────────────────────────────────────────────────────

async def parse_alert_text(text: str) -> ParsedAlert:
    model = llm.get_chat_model().with_structured_output(ParsedAlert)
    return await model.ainvoke([SystemMessage(content=PARSE_INSTRUCTIONS), HumanMessage(content=text)])


async def read_alert_image(data: bytes, mime_type: str) -> str | None:
    """Transcribe an alert screenshot to text, or None if the image isn't an alert."""
    mime = mime_type.split(";")[0].strip().lower()
    if mime not in SUPPORTED_IMAGES:
        return None
    model = llm.get_vision_model().with_structured_output(ImageReading)
    reading = await model.ainvoke([SystemMessage(content=IMAGE_INSTRUCTIONS), HumanMessage(content=[
        {"type": "text", "text": "Read this image."},
        {"type": "image", "base64": base64.b64encode(data).decode(), "mime_type": mime},
    ])])
    return reading.text.strip() if reading.is_alert and reading.text.strip() else None


# ── matching ─────────────────────────────────────────────────────────────────

def _tokens(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def names_match(party: str, bank_name: str | None) -> bool:
    """"Mama Chidi" matches "MAMA CHIDI OKAFOR"; "Emeka" matches "EMEKA JOHN EZE"."""
    party_tokens = _tokens(party)
    return bool(party_tokens) and party_tokens <= _tokens(bank_name)


def fingerprint(user_id: int, p: ParsedAlert) -> str:
    amount = f"{to_money(p.amount)}"
    if p.reference:
        basis = f"{p.direction}|{amount}|ref:{p.reference.strip().lower()}"
    else:
        basis = f"{p.direction}|{amount}|{p.occurred_on}|{name_key(p.counterparty or '')}|{(p.narration or '').strip().lower()}"
    return hashlib.sha256(f"{user_id}|{basis}".encode()).hexdigest()


async def _open_balances_by_party(db: AsyncSession, user_id: int, direction: str) -> dict[str, tuple[str, Decimal]]:
    """{party_key: (display name, total outstanding)} for debts a payment in this direction could settle."""
    rows = (await db.execute(
        select(TransactionORM.party, TransactionORM.outstanding)
        .where(TransactionORM.user_id == user_id, TransactionORM.is_deleted.is_(False),
               TransactionORM.status == "complete", TransactionORM.outstanding > 0,
               TransactionORM.party.is_not(None),
               TransactionORM.type.in_(SETTLEABLE_BY_DIRECTION[direction]))
    )).all()
    grouped: dict[str, tuple[str, Decimal]] = {}
    for party, outstanding in rows:
        key = name_key(party)
        display, total = grouped.get(key, (party.strip(), Decimal("0")))
        grouped[key] = (display, total + outstanding)
    return grouped


async def confirmed_bank_name(db: AsyncSession, user_id: int, bank_name: str) -> str | None:
    """The party key a bank name was confirmed as, or None. party_aliases is the one store
    of bank names (the graph only mirrors it), so this works with graph memory off."""
    return (await db.execute(
        select(Party.key)
        .join(PartyAlias, PartyAlias.party_id == Party.id)
        .where(PartyAlias.user_id == user_id, PartyAlias.alias_key == name_key(bank_name))
    )).scalar_one_or_none()


async def matching_debtors(db: AsyncSession, user_id: int, alert: BankAlert) -> dict[str, tuple[str, Decimal]]:
    """Open balances whose party this alert's sender/recipient could be. A bank name the
    trader confirmed before is a single, certain match."""
    open_parties = await _open_balances_by_party(db, user_id, alert.direction)
    if alert.counterparty:
        key = await confirmed_bank_name(db, user_id, alert.counterparty)
        if key and key in open_parties:
            return {key: open_parties[key]}
    bank_name = alert.counterparty or alert.narration
    return {k: v for k, v in open_parties.items() if names_match(v[0], bank_name)}


async def possible_duplicate(db: AsyncSession, user_id: int, alert: BankAlert) -> TransactionORM | None:
    """An entry the trader may already have typed in for this same money."""
    day = alert.occurred_on or business_today()
    same_money_types = ("sale", "debt_repayment", "loan_taken") if alert.direction == "in" \
        else ("expense", "debt_repayment", "loan_given")
    already_linked = select(BankAlert.transaction_id).where(
        BankAlert.user_id == user_id, BankAlert.transaction_id.is_not(None))
    rows = (await db.execute(
        select(TransactionORM)
        .where(TransactionORM.user_id == user_id, TransactionORM.is_deleted.is_(False),
               TransactionORM.status == "complete", TransactionORM.direction == alert.direction,
               TransactionORM.type.in_(same_money_types), TransactionORM.amount == alert.amount,
               TransactionORM.date.between(day - DUPLICATE_WINDOW, day + DUPLICATE_WINDOW),
               TransactionORM.id.not_in(already_linked))
        .order_by(TransactionORM.date.desc(), TransactionORM.id.desc())
    )).scalars().all()
    bank_name = alert.counterparty or alert.narration
    for row in rows:
        if row.party is None or bank_name is None or names_match(row.party, bank_name):
            return row
    return None


# ── review: what to do with a forwarded alert ────────────────────────────────

def describe_alert(alert: BankAlert) -> str:
    who = alert.counterparty or alert.narration or "an unnamed account"
    arrow = f"in from {who}" if alert.direction == "in" else f"out to {who}"
    when = f" on {alert.occurred_on.strftime('%b %d')}" if alert.occurred_on else ""
    return f"{format_naira(alert.amount)} {arrow}{when}"


async def save_alias(db: AsyncSession, user_id: int, bank_name: str, party: str) -> None:
    """Remember that this bank name is this party (the trader just said so)."""
    key = name_key(bank_name)
    if not key or key == name_key(party):
        return
    party_id = await party_id_for(db, user_id, party)
    alias = (await db.execute(
        select(PartyAlias).where(PartyAlias.user_id == user_id, PartyAlias.alias_key == key)
    )).scalar_one_or_none()
    if alias is None:
        db.add(PartyAlias(user_id=user_id, alias_key=key, party=party.strip(), party_id=party_id))
    else:
        alias.party, alias.party_id = party.strip(), party_id
    await db.flush()


async def _record(db: AsyncSession, user_id: int, alert_id: int, entry: TransactionInput,
                  status: str = "recorded") -> dict:
    result = await persist_transactions(db, [entry], user_id)  # commits
    alert = await db.get(BankAlert, alert_id)                  # re-load: the commit expired it
    alert.status = status
    alert.transaction_id = result["logged"][0]["id"]
    await db.commit()
    return result


async def review_alert(db: AsyncSession, user_id: int, text: str, parsed: ParsedAlert | None = None) -> dict:
    p = parsed or await parse_alert_text(text)
    if p.contains_secret:
        return {"status": "sensitive",
                "message": "That message contains a PIN, OTP, card number or BVN. I didn't save it. "
                           "Never share those with anyone — including me."}
    if not p.is_alert or p.kind == "other" or p.amount is None or p.direction is None or p.amount <= 0:
        return {"status": "not_an_alert"}

    fp = fingerprint(user_id, p)
    existing = (await db.execute(
        select(BankAlert).where(BankAlert.user_id == user_id, BankAlert.fingerprint == fp)
    )).scalar_one_or_none()
    if existing is not None:
        linked = await db.get(TransactionORM, existing.transaction_id) if existing.transaction_id else None
        if existing.status == "pending":
            return {"status": "needs_answer", "alert_id": existing.id, "alert": describe_alert(existing),
                    "question": "You sent this alert before and I'm still waiting to know what it was."}
        if linked is None or not linked.is_deleted:
            return {"status": "duplicate_alert", "alert": describe_alert(existing),
                    "message": "I've already handled this exact alert, so I didn't record it again."}
        await db.delete(existing)  # the entry it created was undone; treat the alert as new
        await db.flush()

    alert = BankAlert(
        user_id=user_id, fingerprint=fp, kind=p.kind, direction=p.direction, amount=to_money(p.amount),
        counterparty=(p.counterparty or "").strip()[:120] or None, narration=(p.narration or "").strip()[:255] or None,
        occurred_on=p.occurred_on, reference=(p.reference or "").strip()[:120] or None, status="pending",
    )
    db.add(alert)
    await db.flush()
    alert_id, summary = alert.id, describe_alert(alert)

    def ask(question: str, **extra) -> dict:
        return {"status": "needs_answer", "alert_id": alert_id, "alert": summary, "question": question, **extra}

    if p.kind == "sender_receipt":
        result = ask("This looks like the sender's 'transfer successful' receipt, not an alert from your own bank — "
                     "it doesn't prove the money arrived. Check your account first. Once it's there, tell me what "
                     "it was for.", warning="possible_fake_alert")
        await db.commit()
        return result

    duplicate = await possible_duplicate(db, user_id, alert)
    if duplicate is not None:
        what = f"{duplicate.type.replace('_', ' ')} {duplicate.item or duplicate.party or ''}".strip()
        result = ask(f"Is this the same money as #{duplicate.id} ({what}, {format_naira(duplicate.amount)}, "
                     f"{duplicate.date.strftime('%b %d')}) that's already recorded?",
                     possible_duplicate_of=duplicate.id)
        await db.commit()
        return result

    matches = await matching_debtors(db, user_id, alert)
    if len(matches) == 1:
        display, owed = next(iter(matches.values()))
        if alert.amount <= owed:
            direction = alert.direction  # read now: _record commits, which expires `alert`
            entry = TransactionInput(type="debt_repayment", direction=direction, party=display,
                                     amount=float(alert.amount), date=alert.occurred_on,
                                     note=f"bank alert: {alert.narration}" if alert.narration else "bank alert")
            result = await _record(db, user_id, alert_id, entry)
            who = f"{display}'s payment" if direction == "in" else f"your payment to {display}"
            return {"status": "recorded", "alert": summary, "recorded_as": who,
                    "notes": result["notes"], "undo_hint": "If that's wrong, the user can say 'cancel that'."}

    options = []
    if matches:
        names = ", ".join(f"{d} (owes {format_naira(o)})" if alert.direction == "in" else f"{d} (you owe {format_naira(o)})"
                          for d, o in matches.values())
        options.append(f"possibly from: {names}" if alert.direction == "in" else f"possibly to: {names}")
    if alert.direction == "in":
        question = "Was this a new sale (what was sold?), a payment of an old debt (whose?), a loan, or not business money?"
    else:
        question = "Was this a business expense (what for?), a payment of something you owed (to whom?), a loan you gave, or personal?"
    result = ask(question, hints=options)
    await db.commit()
    return result


RecordAs = Literal["sale", "expense", "debt_repayment", "loan_taken", "loan_given", "already_recorded", "ignore"]
_DIRECTION_OF = {"sale": "in", "loan_taken": "in", "expense": "out", "loan_given": "out"}


class AlertError(ValueError):
    """Phrased for the user."""


async def resolve_alert(db: AsyncSession, user_id: int, alert_id: int, record_as: RecordAs,
                        party: str | None = None, item: str | None = None,
                        transaction_id: int | None = None) -> dict:
    alert = await db.get(BankAlert, alert_id)
    if alert is None or alert.user_id != user_id or alert.status != "pending":
        raise AlertError("I couldn't find that alert waiting for an answer.")
    direction, amount, occurred_on = alert.direction, alert.amount, alert.occurred_on
    bank_name, narration, summary = alert.counterparty, alert.narration, describe_alert(alert)

    if record_as == "ignore":
        alert.status = "ignored"
        await db.commit()
        return {"status": "ignored", "alert": summary}

    if record_as == "already_recorded":
        row = await db.get(TransactionORM, transaction_id) if transaction_id else None
        if row is None or row.user_id != user_id or row.is_deleted:
            raise AlertError("Which entry is it the same as? I need its number.")
        alert.status = "already_recorded"
        alert.transaction_id = row.id
        await db.commit()
        return {"status": "already_recorded", "alert": summary, "same_as": transaction_id}

    expected = _DIRECTION_OF.get(record_as)
    if expected and expected != direction:
        way = "came in" if direction == "in" else "went out"
        raise AlertError(f"That money {way}, so it can't be recorded as a {record_as.replace('_', ' ')}.")
    if record_as == "debt_repayment" and not party:
        raise AlertError("Whose debt did this pay?")

    if party and bank_name:
        await save_alias(db, user_id, bank_name, party)
    entry = TransactionInput(type=record_as, direction=direction, party=party, item=item, amount=float(amount),
                             date=occurred_on, note=f"bank alert: {narration}" if narration else "bank alert")
    result = await _record(db, user_id, alert_id, entry)
    out = {"status": "recorded", "alert": summary, "recorded": result["logged"][0], "notes": result["notes"]}
    if party and bank_name:
        out["bank_name"] = bank_name  # for graph memory: this bank name is this party
    return out


async def pending_alerts(db: AsyncSession, user_id: int, limit: int = 3) -> list[str]:
    rows = (await db.execute(
        select(BankAlert)
        .where(BankAlert.user_id == user_id, BankAlert.status == "pending")
        .order_by(BankAlert.created_at.desc(), BankAlert.id.desc())
        .limit(limit)
    )).scalars().all()
    return [f"#{a.id}: {describe_alert(a)}" for a in rows]


if __name__ == "__main__":
    # check parsing against real alerts:
    #   uv run python -m tools.alerts "Credit Alert! Amt: NGN10,000.00 ..."     (text)
    #   uv run python -m tools.alerts screenshot.jpg                              (image)
    import asyncio
    import mimetypes
    import os
    import sys

    async def _main(args: list[str]) -> None:
        for arg in args:
            if os.path.isfile(arg):
                with open(arg, "rb") as f:
                    text = await read_alert_image(f.read(), mimetypes.guess_type(arg)[0] or "")
                print(f"{arg}\n  read as: {text!r}")
                if not text:
                    continue
            else:
                text = arg
            print(f"  parsed:  {(await parse_alert_text(text)).model_dump(exclude_none=True)}\n")

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_main(sys.argv[1:]))

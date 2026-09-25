"""Statement of account between a trader and one customer or supplier.

Every sale, loan and payment with that person in date order, with a running balance —
so when a customer asks "₦5,000 for what?", the trader can show exactly what was bought
and what was paid. A record to share, never a way to pay (the app doesn't touch money).

Sign convention: the balance is what the other person owes the trader. Positive = they
owe the trader; negative = the trader owes them.
"""
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from db import RepaymentAllocation, StatementLink, TransactionORM, User
from tools.contacts import name_key
from tools.debt_collection import business_name
from tools.formatting import format_naira

ZERO = Decimal("0.00")
TEXT_MAX_LINES = 12


class StatementError(ValueError):
    """Phrased for the trader."""


@dataclass
class StatementLine:
    date: date
    description: str
    amount: Decimal       # signed: + they owe more, − they owe less
    balance: Decimal = ZERO
    due: date | None = None  # promised payment date, on the entry that created the debt


@dataclass
class Statement:
    business: str
    party: str
    start: date | None
    end: date | None
    opening_balance: Decimal
    lines: list[StatementLine]
    closing_balance: Decimal
    generated_at: datetime
    pending_amounts: int = 0  # entries with this person still missing an amount
    totals: dict = field(default_factory=dict)

    def balance_sentence(self) -> str:
        if self.closing_balance > 0:
            return f"{self.party} owes {self.business} {format_naira(self.closing_balance)}"
        if self.closing_balance < 0:
            return f"{self.business} owes {self.party} {format_naira(-self.closing_balance)}"
        return f"{self.party} and {self.business} are all settled — nobody owes anything"


def _goods(t: TransactionORM) -> str:
    qty = f"{t.quantity:g} {t.unit or ''}".strip() if t.quantity else ""
    return " ".join(p for p in (qty, t.item) if p) or "goods"


def _entry_lines(t: TransactionORM, upfront: Decimal) -> list[tuple[str, Decimal]]:
    """(description, signed amount) for one ledger entry, including any part paid on the spot."""
    if t.type == "sale":
        lines = [(f"Bought {_goods(t)}", t.amount)]
        if upfront > 0:
            lines.append(("Payment", -upfront))
    elif t.type == "loan_given":
        lines = [("Loan", t.amount)]
        if upfront > 0:
            lines.append(("Loan repayment", -upfront))
    elif t.type == "expense":
        lines = [(f"Supplied {_goods(t)}", -t.amount)]
        if upfront > 0:
            lines.append(("Payment to you", upfront))
    elif t.type == "loan_taken":
        lines = [("Loan to us", -t.amount)]
        if upfront > 0:
            lines.append(("Loan repayment to you", upfront))
    elif t.direction == "in":
        lines = [("Payment", -t.amount)]
    else:
        lines = [("Payment to you", t.amount)]
    if t.note:
        desc, amount = lines[0]
        lines[0] = (f"{desc} — {t.note}", amount)
    return lines


async def build_statement(db: AsyncSession, user: User, party: str,
                          start: date | None = None, end: date | None = None) -> Statement:
    key = name_key(party)
    rows = (await db.execute(
        select(TransactionORM)
        .where(TransactionORM.user_id == user.id, TransactionORM.is_deleted.is_(False),
               func.lower(func.trim(TransactionORM.party)) == key)
        .order_by(TransactionORM.date, TransactionORM.id)
    )).scalars().all()
    if not rows:
        raise StatementError(f"I don't have any dealings with {party} on record.")
    if end is not None:
        rows = [r for r in rows if r.date <= end]

    complete = [r for r in rows if r.status == "complete"]
    pending = len(rows) - len(complete)

    # what was paid on the spot = amount_paid minus what later repayments contributed
    allocated: dict[int, Decimal] = defaultdict(lambda: ZERO)
    if complete:
        for applied_to, total in (await db.execute(
            select(RepaymentAllocation.applied_to_id, func.sum(RepaymentAllocation.amount))
            .where(RepaymentAllocation.applied_to_id.in_([r.id for r in complete]))
            .group_by(RepaymentAllocation.applied_to_id)
        )).all():
            allocated[applied_to] = Decimal(total)

    opening = ZERO
    balance = ZERO
    lines: list[StatementLine] = []
    charged = paid = ZERO
    for t in complete:
        upfront = max((t.amount_paid or ZERO) - allocated[t.id], ZERO) if t.type != "debt_repayment" else ZERO
        for i, (desc, amount) in enumerate(_entry_lines(t, upfront)):
            balance += amount
            if start is not None and t.date < start:
                opening = balance  # brought forward
                continue
            due = t.due_date if i == 0 and t.type in ("sale", "loan_given") else None
            lines.append(StatementLine(t.date, desc, amount, balance, due))
            if amount > 0:
                charged += amount
            else:
                paid += -amount

    display_name = next((r.party.strip() for r in rows if r.party), party)
    return Statement(
        business=business_name(user), party=display_name, start=start, end=end,
        opening_balance=opening, lines=lines, closing_balance=balance,
        generated_at=datetime.now(timezone.utc), pending_amounts=pending,
        totals={"added": charged, "reduced": paid},
    )


def _signed(amount: Decimal) -> str:
    return ("+" if amount > 0 else "−") + format_naira(abs(amount))


def as_text(st: Statement, max_lines: int = TEXT_MAX_LINES) -> str:
    """Short version for chat — easy to read on a phone and forward on WhatsApp."""
    out = [f"Statement: {st.party} — {st.business}"]
    shown = st.lines[-max_lines:]
    hidden = len(st.lines) - len(shown)
    if st.opening_balance or hidden:
        brought = st.lines[hidden - 1].balance if hidden else st.opening_balance
        out.append(f"Brought forward: {format_naira(brought)}")
    for line in shown:
        out.append(f"{line.date.strftime('%b %d')} · {line.description} · {_signed(line.amount)}")
    out.append(f"Balance: {st.balance_sentence()}.")
    if st.pending_amounts:
        out.append(f"({st.pending_amounts} entr{'y' if st.pending_amounts == 1 else 'ies'} still need an amount and aren't included.)")
    return "\n".join(out)


# ── share links ──────────────────────────────────────────────────────────────

LINK_LIFETIME = timedelta(days=30)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


def link_url(token: str) -> str | None:
    base = settings.APP_PUBLIC_URL.rstrip("/")
    return f"{base}/s/{token}" if base else None


async def share_link(db: AsyncSession, user: User, party: str) -> str:
    """Reuse the active link for this person (pushing its expiry out), or make a new one.
    Returns the link's token."""
    key = name_key(party)
    now = datetime.now(timezone.utc)
    link = (await db.execute(
        select(StatementLink)
        .where(StatementLink.user_id == user.id, StatementLink.party_key == key,
               StatementLink.revoked.is_(False), StatementLink.expires_at > now)
        .order_by(StatementLink.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if link is None:
        link = StatementLink(token=secrets.token_urlsafe(24), user_id=user.id, party=party.strip(), party_key=key,
                             expires_at=now + LINK_LIFETIME, revoked=False)
        db.add(link)
    else:
        link.expires_at = now + LINK_LIFETIME
    token = link.token  # committed objects are expired and can't lazy-load in async
    await db.commit()
    return token


async def revoke_links(db: AsyncSession, user_id: int, party: str) -> int:
    links = (await db.execute(
        select(StatementLink)
        .where(StatementLink.user_id == user_id, StatementLink.party_key == name_key(party),
               StatementLink.revoked.is_(False))
    )).scalars().all()
    for link in links:
        link.revoked = True
    await db.commit()
    return len(links)


async def resolve_link(db: AsyncSession, token: str) -> StatementLink | None:
    link = (await db.execute(select(StatementLink).where(StatementLink.token == token))).scalar_one_or_none()
    if link is None or link.revoked or _aware(link.expires_at) <= datetime.now(timezone.utc):
        return None
    return link

"""The Collector's memory work: which promises are broken, and who to chase first, and why.

All of it is code, not the model, so the ranking is the same every time and every reason
can be traced. The model only phrases the answer.

    sweep()        open promises past their due date → broken (with how much was paid), or
                   kept if the debt was in fact cleared on time and the sync missed it
    who_to_chase() open debts from Postgres + promise history and do-not-remind from the
                   graph → a ranked list with reasons, and who was skipped and why

Ranking: most broken promises first, then longest overdue, then most owed, then owed
longest. A debtor
the trader said not to remind, or who replied STOP, is never in the list: they're shown
as skipped, with the reason. Without graph memory (or for a new trader) the ranking falls
back to overdue and amount only, which is the "day 1" behaviour the demo contrasts with.
"""
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from config import business_today
from db import Contact, ReminderOptOut, TransactionORM
from memory.models import GraphMemoryError, PartyMemory
from memory.ports import GraphMemory
from memory.sync import SessionFactory
from tools.formatting import format_naira
from utils import logger

AGENT = "collector"
MAX_LISTED = 5


@dataclass
class Debtor:
    name: str
    key: str
    owed: Decimal
    since: date
    overdue_since: date | None = None   # earliest missed due date still unpaid
    kept: int = 0
    broken: int = 0
    reasons: list[str] = field(default_factory=list)

    def sort_key(self, today: date):
        overdue_days = (today - self.overdue_since).days if self.overdue_since else 0
        return (-self.broken, -overdue_days, -self.owed, self.since)  # ties: owed longest first


@dataclass
class Skipped:
    name: str
    owed: Decimal
    reason: str


def _short(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%b} {d.day}"


def _do_not_remind(memory: PartyMemory | None, today: date):
    """The do-not-remind fact still in force, if any (an `until` in the past has lapsed)."""
    for f in (memory.facts if memory else ()):
        if f.kind == "do_not_remind" and not (f.until and date.fromisoformat(f.until) < today):
            return f
    return None


class Collector:
    def __init__(self, memory: GraphMemory, session_factory: SessionFactory):
        self._memory = memory
        self._session_factory = session_factory

    async def sweep(self, user_id: int, today: date | None = None) -> dict:
        """Settle the outcome of every open promise whose day has passed. Idempotent."""
        today = today or business_today()
        counts = {"broken": 0, "kept": 0}
        if not self._memory.enabled:
            return counts
        for p in await self._memory.overdue_promises(user_id, today):
            if p.debt_open:
                status, fraction = "broken", await self._paid_fraction(p.pg_id)
            elif p.settled_on and p.settled_on <= p.due:
                status, fraction = "kept", 1.0       # cleared on time; the sync's mark was missed
            else:
                status, fraction = "broken", 1.0     # paid in the end, but late
            await self._memory.set_promise_status(user_id, p.promise_id, status, fraction, today, by=AGENT)
            counts[status] += 1
        if counts["broken"] or counts["kept"]:
            logger.info("collector.swept", user_id=user_id, **counts)
        return counts

    async def _paid_fraction(self, pg_id: int) -> float | None:
        async with self._session_factory() as db:
            row = await db.get(TransactionORM, pg_id)
            if row is None or not row.amount:
                return None
            return round(float((row.amount_paid or 0) / row.amount), 2)

    async def _open_debts(self, user_id: int) -> dict[str, Debtor]:
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(TransactionORM)
                .where(TransactionORM.user_id == user_id, TransactionORM.is_deleted.is_(False),
                       TransactionORM.status == "complete", TransactionORM.outstanding > 0,
                       TransactionORM.party.is_not(None), TransactionORM.type.in_(("sale", "loan_given")))
                .order_by(TransactionORM.date, TransactionORM.id)
            )).scalars().all()
        debtors: dict[str, Debtor] = {}
        for r in rows:
            key = " ".join(r.party.lower().split())
            d = debtors.get(key)
            if d is None:
                d = debtors[key] = Debtor(name=r.party.strip(), key=key, owed=Decimal("0"), since=r.date)
            d.name = r.party.strip()  # latest spelling
            d.owed += r.outstanding
            if r.due_date and (d.overdue_since is None or r.due_date < d.overdue_since):
                d.overdue_since = r.due_date
        return debtors

    async def _opted_out(self, user_id: int, keys: list[str]) -> set[str]:
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(Contact.name_key)
                .join(ReminderOptOut, ReminderOptOut.phone == Contact.phone)
                .where(Contact.user_id == user_id, func.lower(Contact.name_key).in_(keys))
            )).scalars().all()
        return {" ".join(k.lower().split()) for k in rows}

    async def who_to_chase(self, user_id: int, today: date | None = None) -> dict:
        today = today or business_today()
        started = time.perf_counter()
        try:
            swept = await self.sweep(user_id, today)
        except GraphMemoryError as e:
            logger.warning("collector.sweep_failed", user_id=user_id, error=str(e))
            swept = {}
        debtors = await self._open_debts(user_id)
        for d in debtors.values():
            if d.overdue_since and d.overdue_since >= today:
                d.overdue_since = None  # a due date still ahead isn't overdue

        memories: dict[str, PartyMemory] = {}
        if self._memory.enabled and debtors:
            try:
                memories = {m.key: m for m in await self._memory.recall(user_id, list(debtors))}
            except GraphMemoryError as e:
                logger.warning("collector.recall_failed", user_id=user_id, error=str(e))
        opted_out = await self._opted_out(user_id, list(debtors)) if debtors else set()

        chase: list[Debtor] = []
        skipped: list[Skipped] = []
        for key, d in debtors.items():
            m = memories.get(key)
            if key in opted_out:
                skipped.append(Skipped(d.name, d.owed, "replied STOP to reminders"))
                continue
            stop = _do_not_remind(m, today)
            if stop:
                when = f" on {_short(stop.since[:10])}" if stop.since else ""
                said = f' ("{stop.said}")' if stop.said else ""
                skipped.append(Skipped(d.name, d.owed, f"you said{when} not to remind them{said}"))
                continue
            if m:
                d.kept, d.broken = m.kept, m.broken
            if d.overdue_since:
                d.reasons.append(f"promised to pay by {_short(d.overdue_since)} and hasn't "
                                 f"({(today - d.overdue_since).days} days ago)")
            if d.broken:
                d.reasons.append(f"has broken {d.broken} of {d.kept + d.broken} promises")
            elif d.kept:
                d.reasons.append(f"kept all {d.kept} promises so far")
            d.reasons.append(f"owes {format_naira(d.owed)} since {_short(d.since)}")
            chase.append(d)

        chase.sort(key=lambda d: d.sort_key(today))
        result = {
            "chase": [{"name": d.name, "owes": format_naira(d.owed), "reasons": d.reasons}
                      for d in chase[:MAX_LISTED]],
            "skipped": [{"name": s.name, "owes": format_naira(s.owed), "reason": s.reason} for s in skipped],
            "ranked_by": "broken promises, then days overdue, then amount owed"
                         if memories else "days overdue, then amount owed (nothing remembered yet)",
        }
        if len(chase) > MAX_LISTED:
            result["more"] = len(chase) - MAX_LISTED
        logger.info("collector.ranked", user_id=user_id, debtors=len(debtors), listed=len(result["chase"]),
                    skipped=len(skipped), with_memory=bool(memories), swept=swept,
                    ms=round((time.perf_counter() - started) * 1000, 1))
        return result

    async def do_not_remind(self, user_id: int, party_key: str, today: date | None = None) -> str | None:
        """Why not to remind this party, in the trader's words, or None."""
        if not self._memory.enabled:
            return None
        try:
            fact = await self._memory.current_fact(user_id, party_key, "do_not_remind")
        except GraphMemoryError as e:
            logger.warning("collector.check_failed", user_id=user_id, error=str(e))
            return None
        today = today or business_today()
        if not fact or (fact["until"] and date.fromisoformat(fact["until"]) < today):
            return None
        when = f" on {_short(fact['since'][:10])}" if fact.get("since") else ""
        said = f' ("{fact["said"]}")' if fact.get("said") else ""
        return f"you said{when} not to remind them{said}"

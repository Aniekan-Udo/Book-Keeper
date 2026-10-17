"""Keeps the graph in step with the ledger.

Postgres is the source of truth for money; the graph mirrors only structure: which
parties exist, which entries are (or were) owed and whether they're still open, and who is
a customer. This service reads the ledger and hands snapshots to the GraphMemory port.

It runs in two ways:
- `on_ledger_changed`: subscribed to tools/ledger_events, after every committed change,
  for just the parties that changed.
- `rebuild`: everything for one trader, from scripts/rebuild_graph.py, to recover after
  the graph was down or wiped. Learned memory (aliases, facts) is never touched.
"""
import asyncio
import time
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import Party, PartyAlias, RepaymentAllocation, TransactionORM
from memory.models import DebtSnapshot, GraphMemoryError, PartySnapshot, Source
from memory.ports import GraphMemory
from tools.ledger_events import LedgerChanged
from utils import logger

RECEIVABLE_TYPES = ("sale", "loan_given")   # the party owes the trader
PAYABLE_TYPES = ("expense", "loan_taken")   # the trader owes the party

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_PARTY_KEY = func.lower(func.trim(TransactionORM.party))  # same grouping as tools/ledger


def _is_debt(row: TransactionORM, ever_repaid: set[int]) -> bool:
    """A cash sale isn't a debt. An entry is one if something is still owed, a repayment
    was ever applied to it, or someone promised a date for it."""
    if row.status != "complete" or row.outstanding is None:
        return False
    return row.outstanding > 0 or row.id in ever_repaid or row.due_date is not None


async def party_snapshots(db: AsyncSession, user_id: int,
                          keys: Iterable[str] | None = None) -> list[PartySnapshot]:
    """What the ledger says about these parties now (all of the trader's parties if
    `keys` is None). A key with no entries left comes back with name=None."""
    query = (
        select(TransactionORM)
        .where(TransactionORM.user_id == user_id,
               TransactionORM.is_deleted.is_(False),
               TransactionORM.party.is_not(None),
               TransactionORM.type.in_(RECEIVABLE_TYPES + PAYABLE_TYPES))
        .order_by(TransactionORM.date, TransactionORM.id)
    )
    wanted = None
    if keys is not None:
        wanted = {k for k in keys if k}
        if not wanted:
            return []
        query = query.where(_PARTY_KEY.in_(wanted))
    rows = (await db.execute(query)).scalars().all()

    ids = [r.id for r in rows]
    # debt id → date of the latest repayment applied to it (when it was cleared)
    last_repaid: dict[int, date] = dict((await db.execute(
        select(RepaymentAllocation.applied_to_id, func.max(TransactionORM.date))
        .join(TransactionORM, TransactionORM.id == RepaymentAllocation.repayment_id)
        .where(RepaymentAllocation.applied_to_id.in_(ids), TransactionORM.is_deleted.is_(False))
        .group_by(RepaymentAllocation.applied_to_id)
    )).all()) if ids else {}
    ever_repaid = set(last_repaid)

    by_key: dict[str, list[TransactionORM]] = {}
    for row in rows:
        key = row.party.strip().lower()
        if key:
            by_key.setdefault(key, []).append(row)

    snapshots = []
    for key, party_rows in by_key.items():
        receivables = [r for r in party_rows if r.type in RECEIVABLE_TYPES]
        snapshots.append(PartySnapshot(
            key=key,
            name=party_rows[-1].party.strip(),  # rows are oldest first: the latest spelling wins
            debts=tuple(
                DebtSnapshot(pg_id=r.id, kind="receivable" if r.type in RECEIVABLE_TYPES else "payable",
                             open=r.outstanding > 0, date=r.date, due=r.due_date,
                             # paid upfront but given a date anyway: cleared on its own date
                             settled_on=None if r.outstanding > 0 else last_repaid.get(r.id, r.date))
                for r in party_rows if _is_debt(r, ever_repaid)
            ),
            customer_since=receivables[0].date if receivables else None,
            pg_id=party_rows[-1].party_id,
        ))
    for key in (wanted or set()) - by_key.keys():
        snapshots.append(PartySnapshot(key=key, name=None))
    return snapshots


class GraphSync:
    def __init__(self, memory: GraphMemory, session_factory: SessionFactory, retry_delay: float = 10.0):
        self._memory = memory
        self._session_factory = session_factory
        self._retry_delay = retry_delay

    async def run(self, event: LedgerChanged) -> int:
        """One attempt: mirror these parties. Returns how many were synced; raises
        GraphMemoryError if the store fails (the job queue retries it)."""
        if not self._memory.enabled:
            return 0
        async with self._session_factory() as db:
            snapshots = await party_snapshots(db, event.user_id, event.party_keys)
        await self._memory.sync_parties(event.user_id, snapshots)
        return len(snapshots)

    async def handle_job(self, payload: dict, user_id: int) -> None:
        """A queued `ledger_changed` job (jobs/queue.py)."""
        await self.run(LedgerChanged(user_id=user_id, party_keys=frozenset(payload["party_keys"]),
                                     reason=payload.get("reason", "recorded")))

    async def on_ledger_changed(self, event: LedgerChanged) -> None:
        """The in-process delivery (tools/ledger_events): one retry, then give up quietly."""
        if not self._memory.enabled:
            return
        for attempt in (1, 2):
            started = time.perf_counter()
            try:
                # re-read on each attempt: the retry should mirror the ledger as it is then
                synced = await self.run(event)
            except GraphMemoryError as e:
                # the ledger is fine either way. One retry covers a blip; after that the graph
                # is behind until the next change to these parties or a rebuild
                logger.warning("graph_sync.failed", user_id=event.user_id, reason=event.reason, attempt=attempt,
                               parties=len(event.party_keys), error=str(e))
                if attempt == 1:
                    await asyncio.sleep(self._retry_delay)
                continue
            logger.info("graph_sync.done", user_id=event.user_id, reason=event.reason, attempt=attempt,
                        parties=synced, ms=round((time.perf_counter() - started) * 1000, 1))
            return

    async def mirror_bank_names(self, user_id: int) -> int:
        """Copy confirmed bank names (party_aliases, their one store) into the graph where it
        doesn't have them yet: after a wipe, or ones confirmed before the graph existed.
        A name the graph already links to someone is left alone. Returns how many were added."""
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(PartyAlias.alias_key, Party.key)
                .join(Party, Party.id == PartyAlias.party_id)
                .where(PartyAlias.user_id == user_id)
            )).all()
        if not rows:
            return 0
        names = await self._memory.known_names(user_id)
        taken = {n.key for n in names}
        parties = {n.party_key for n in names}
        source = Source(agent="ledger_sync", at=datetime.now(timezone.utc).isoformat(), turn_id=None)
        added = 0
        for alias_key, party_key in rows:
            key = " ".join(alias_key.split())  # graph alias keys collapse inner spaces
            if key in taken or party_key not in parties:
                continue
            await self._memory.add_alias(user_id, key, alias_key.upper(), party_key, "bank_name", source)
            taken.add(key)
            added += 1
        return added

    async def rebuild(self, user_id: int) -> dict:
        """Resync every party of one trader and drop debts the ledger no longer has.
        Raises GraphMemoryError if the store fails part-way (safe to run again)."""
        async with self._session_factory() as db:
            snapshots = await party_snapshots(db, user_id)
        await self._memory.sync_parties(user_id, snapshots)
        alive = [d.pg_id for s in snapshots for d in s.debts]
        removed = await self._memory.prune_debts(user_id, alive)
        bank_names = await self.mirror_bank_names(user_id)
        summary = {"user_id": user_id, "parties": len(snapshots), "debts": len(alive), "removed": removed,
                   "bank_names": bank_names}
        logger.info("graph_sync.rebuilt", **summary)
        return summary

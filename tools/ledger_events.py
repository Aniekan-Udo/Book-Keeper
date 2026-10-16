"""What the ledger tells the rest of the app when it commits a change.

The ledger announces `LedgerChanged` and knows nothing about who listens. Today the graph
memory (memory/sync.py) listens, to keep parties and debts in FalkorDB in step with
Postgres. This keeps the money rules free of any graph code: if the graph is down or
removed, recording a sale works exactly the same.

The ledger calls `stage(db, event)` before its commit and `committed(event)` after. How
the event travels depends on the delivery in use:
- in-process (default; tests and scripts): `committed` runs the handlers as background
  tasks. A slow or failing handler never delays or breaks the reply, but a crash loses it.
- outbox (the app, with graph memory on; main.py): `stage` writes a job into the ledger's
  own transaction (jobs/queue.py), so the event is saved exactly when the change is, and a
  worker delivers it, retrying until it succeeds. `committed` just wakes the worker.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from utils import logger


@dataclass(frozen=True)
class LedgerChanged:
    user_id: int
    # normalised party names (tools/ledger._party_key) whose entries changed
    party_keys: frozenset[str]
    reason: str  # "recorded" / "deleted" / "corrected", for logs


Handler = Callable[[LedgerChanged], Awaitable[None]]


class LedgerEventBus:
    def __init__(self):
        self._handlers: list[Handler] = []
        # strong references: asyncio only keeps weak ones, so a running task could be collected
        self._tasks: set[asyncio.Task] = set()
        self._outbox: tuple[Callable, Callable[[], None]] | None = None  # (stage job, wake worker)

    def use_outbox(self, stage_job: Callable | None, wake: Callable[[], None] | None = None) -> None:
        """Deliver through the durable job queue (or, with None, back to in-process)."""
        self._outbox = (stage_job, wake) if stage_job else None

    def stage(self, db, event: LedgerChanged) -> None:
        """Before the ledger's commit: with the outbox, the event becomes a job in the same
        transaction. Never raises past the ledger for a missing listener."""
        if self._outbox and event.party_keys:
            stage_job, _ = self._outbox
            stage_job(db, "ledger_changed", event.user_id,
                      {"party_keys": sorted(event.party_keys), "reason": event.reason})

    def committed(self, event: LedgerChanged) -> None:
        """After the ledger's commit. Never raises: the change is already saved."""
        if self._outbox:
            self._outbox[1]()
        else:
            self.publish(event)

    def subscribe(self, handler: Handler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def publish(self, event: LedgerChanged) -> None:
        """Schedule every handler. Never raises: the ledger has already committed."""
        if not event.party_keys or not self._handlers:
            return
        for handler in self._handlers:
            try:
                task = asyncio.get_running_loop().create_task(self._run(handler, event))
            except RuntimeError:
                # no running loop (a sync caller) — nothing can run the handler
                logger.warning("ledger_event.no_loop", handler=_name(handler), user_id=event.user_id)
                continue
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for handlers already scheduled. For shutdown, scripts and tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @staticmethod
    async def _run(handler: Handler, event: LedgerChanged) -> None:
        started = time.perf_counter()
        try:
            await handler(event)
        except Exception:
            logger.error("ledger_event.handler_failed", handler=_name(handler), user_id=event.user_id,
                         reason=event.reason, parties=len(event.party_keys), exc_info=True)
            return
        logger.debug("ledger_event.handled", handler=_name(handler), user_id=event.user_id,
                     ms=round((time.perf_counter() - started) * 1000, 1))


def _name(handler: Handler) -> str:
    return getattr(handler, "__qualname__", repr(handler))


ledger_events = LedgerEventBus()

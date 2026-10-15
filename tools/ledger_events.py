"""What the ledger tells the rest of the app after it commits a change.

The ledger publishes `LedgerChanged` and knows nothing about who listens. Today the graph
memory (memory/sync.py) listens, to keep parties and debts in FalkorDB in step with
Postgres. This keeps the money rules free of any graph code: if the graph is down or
removed, recording a sale works exactly the same.

Handlers run as background tasks after the commit, so a slow or failing listener never
delays or breaks the reply. Every handler failure is logged, never raised.
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

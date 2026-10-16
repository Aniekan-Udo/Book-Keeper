"""One conversation turn at a time per user, across every process and server.

Two quick messages from the same trader must not run side by side: both would read the
same history and pending items, and their replies (and ledger entries) could interleave.

- An in-process asyncio lock is the cheap first gate: messages arriving at the same
  process queue up without touching the database.
- A Postgres advisory lock covers every other process and server. It's taken on its own
  connection and held for the whole turn, so the commits the ledger makes mid-turn don't
  release it, and if the process dies Postgres releases it with the connection.

Waiting is bounded: if the previous turn is still running after TURN_LOCK_TIMEOUT seconds,
the new message gets "still working on your last message" instead of hanging. On SQLite
(tests, local scripts) there are no advisory locks, and the in-process lock is enough.
"""
import asyncio
import os
import time
import weakref
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tools.router import LLMUnavailable
from utils import logger

LOCK_NAMESPACE = 0x424B  # "BK": keeps these locks apart from any other advisory locks in the database
# a little longer than one agent run's ceiling (LLM_TURN_TIMEOUT, 90 s by default)
TURN_LOCK_TIMEOUT = float(os.getenv("TURN_LOCK_TIMEOUT", "120"))
LOCK_NOT_AVAILABLE = "55P03"  # Postgres: lock_timeout expired

_local_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


class TurnInProgress(LLMUnavailable):
    """The user's previous message is still being worked on. Both channels already show
    LLMUnavailable as a friendly "try again shortly"."""


def _engine_of(db: AsyncSession) -> AsyncEngine | None:
    bind = db.bind
    if bind is None:
        return None
    return bind if isinstance(bind, AsyncEngine) else bind.engine  # a session bound to a connection (tests)


@asynccontextmanager
async def user_turn_lock(db: AsyncSession, user_id: int, timeout: float | None = None):
    timeout = TURN_LOCK_TIMEOUT if timeout is None else timeout
    local = _local_locks.get(user_id)
    if local is None:
        local = _local_locks[user_id] = asyncio.Lock()
    try:
        await asyncio.wait_for(local.acquire(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("turn_lock.busy", user_id=user_id, where="process")
        raise TurnInProgress("I'm still working on your last message. Give me a moment, then send this again.")
    try:
        engine = _engine_of(db)
        if engine is None or engine.dialect.name != "postgresql":
            yield
            return
        async with _advisory_lock(engine, user_id, timeout):
            yield
    finally:
        local.release()


@asynccontextmanager
async def _advisory_lock(engine: AsyncEngine, user_id: int, timeout: float):
    started = time.perf_counter()
    async with engine.connect() as conn:
        try:
            # local to this transaction (is_local=true), so the pooled connection doesn't keep
            # the timeout; the advisory lock is session-level and outlives the commit
            await conn.execute(text("SELECT set_config('lock_timeout', :ms, true)"),
                               {"ms": f"{int(timeout * 1000)}ms"})
            await conn.execute(text("SELECT pg_advisory_lock(:ns, :uid)"), {"ns": LOCK_NAMESPACE, "uid": int(user_id)})
            await conn.commit()
        except DBAPIError as e:
            await conn.rollback()
            if LOCK_NOT_AVAILABLE in str(getattr(e.orig, "sqlstate", "")) or "lock timeout" in str(e).lower():
                logger.warning("turn_lock.busy", user_id=user_id, where="database", waited_s=round(timeout))
                raise TurnInProgress("I'm still working on your last message. Give me a moment, then send this again.") from e
            raise
        waited = time.perf_counter() - started
        if waited > 1:
            logger.info("turn_lock.waited", user_id=user_id, seconds=round(waited, 1))
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"),
                                   {"ns": LOCK_NAMESPACE, "uid": int(user_id)})
                await conn.commit()
            except Exception:  # the connection closing releases it anyway
                logger.warning("turn_lock.unlock_failed", user_id=user_id, exc_info=True)

"""One turn at a time per user: the in-process gate (always runs) and the Postgres advisory
lock across processes (runs when TEST_LOCK_POSTGRES_URL points at a Postgres server; it
takes advisory locks only and touches no tables, so any database will do)."""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tools.turn_lock import TurnInProgress, _advisory_lock, user_turn_lock


async def test_same_user_waits_and_other_users_do_not(db_session):
    order = []

    async def turn(user_id, name, hold):
        async with user_turn_lock(db_session, user_id):
            order.append(f"{name} start")
            await asyncio.sleep(hold)
            order.append(f"{name} end")

    await asyncio.gather(turn(1, "a1", 0.1), turn(1, "a2", 0), turn(2, "b", 0))
    assert order.index("a1 end") < order.index("a2 start")       # same user: one after the other
    assert order.index("b start") < order.index("a1 end")        # another user: not held up


async def test_a_stuck_turn_gives_a_friendly_busy(db_session):
    holding = asyncio.Event()

    async def stuck():
        async with user_turn_lock(db_session, 7):
            holding.set()
            await asyncio.sleep(0.5)

    task = asyncio.create_task(stuck())
    await holding.wait()  # not a fixed delay: on a loaded machine the holder may start late
    with pytest.raises(TurnInProgress, match="still working on your last message"):
        async with user_turn_lock(db_session, 7, timeout=0.1):
            pass
    await task


PG_URL = os.getenv("TEST_LOCK_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not PG_URL, reason="set TEST_LOCK_POSTGRES_URL to test the advisory lock")


@pytest.fixture
async def two_processes():
    """Two engines, each with its own connection pool: as two app processes would be."""
    a, b = create_async_engine(PG_URL), create_async_engine(PG_URL)
    yield a, b
    await a.dispose()
    await b.dispose()


@needs_postgres
async def test_advisory_lock_serialises_one_user_across_processes(two_processes):
    a, b = two_processes
    order = []
    a_holds_it = asyncio.Event()

    async def turn(engine, name, hold, started=None):
        async with _advisory_lock(engine, 424201, timeout=5):
            order.append(f"{name} start")
            if started:
                started.set()
            await asyncio.sleep(hold)
            order.append(f"{name} end")

    first = asyncio.create_task(turn(a, "process A", 0.4, a_holds_it))
    await a_holds_it.wait()  # connecting can take a while; start B only once A has the lock
    await turn(b, "process B", 0)
    await first
    assert order == ["process A start", "process A end", "process B start", "process B end"]


@needs_postgres
async def test_advisory_lock_lets_other_users_through(two_processes):
    a, b = two_processes
    async with _advisory_lock(a, 424202, timeout=5):
        async with _advisory_lock(b, 424203, timeout=1):  # a different user: no wait
            pass


@needs_postgres
async def test_advisory_lock_times_out_as_busy_and_is_released(two_processes):
    a, b = two_processes
    async with _advisory_lock(a, 424204, timeout=5):
        with pytest.raises(TurnInProgress):
            async with _advisory_lock(b, 424204, timeout=0.3):
                pass
    async with _advisory_lock(b, 424204, timeout=1):  # free again once A is done
        pass

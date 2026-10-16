"""The durable job queue: jobs are saved with the change that causes them (or not at all),
run in order per user, retried with backoff, parked as failed after too many attempts, and
picked up again after a crash. Graph sync and curation flow through it end to end.

The cross-process test needs a Postgres server (TEST_JOBS_POSTGRES_URL, a database it may
create a `jobs` table in); everything else runs on the test SQLite database.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db import Job, Model
from jobs import queue as job_queue
from jobs.queue import MAX_ATTEMPTS, JobQueue, RetryLater
from jobs.worker import JobWorker
from tools.ledger import TransactionInput, persist_transactions
from tools.ledger_events import ledger_events


def session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session
    return factory


def worker_for(db_session, handlers):
    return JobWorker(JobQueue(session_factory(db_session)), handlers, db_session.bind.engine)


async def jobs(db_session):
    return (await db_session.execute(select(Job).order_by(Job.id))).scalars().all()


def sale(party):
    return TransactionInput(type="sale", party=party, item="rice", amount=45000, amount_paid=0, direction="in")


@pytest.fixture
def outbox():
    """The ledger delivering through the queue, as in the app."""
    wakes = []
    ledger_events.use_outbox(job_queue.stage, lambda: wakes.append(1))
    yield wakes
    ledger_events.use_outbox(None)


# ── saved with the change, or not at all ─────────────────────────────────────

async def test_a_ledger_change_queues_its_sync_in_the_same_commit(db_session, test_user, outbox):
    await persist_transactions(db_session, [sale("Emeka"), sale("Mama Chidi")], test_user.id)

    [job] = await jobs(db_session)
    assert (job.kind, job.user_id, job.status) == ("ledger_changed", test_user.id, "pending")
    assert job.payload == {"party_keys": ["emeka", "mama chidi"], "reason": "recorded"}
    assert outbox == [1]  # the worker was woken after the commit


async def test_a_rolled_back_change_queues_nothing(db_session, test_user):
    job_queue.stage(db_session, "ledger_changed", test_user.id, {"party_keys": ["emeka"]})
    await db_session.rollback()
    assert await jobs(db_session) == []


async def test_a_reply_queues_its_curation_with_the_saved_turn(db_session, test_user, monkeypatch):
    from memory import set_curator
    from memory.curator import Curator
    from tools import assistant, router

    class On:  # a memory that's "enabled", so the curator stages its work
        enabled = True

        async def known_names(self, user_id):
            return []

        async def recall(self, user_id, keys):
            return []

    async def agent(text, prompt, tools, history=None, callbacks=None):
        return {"messages": [AIMessage(content="OK.")]}

    monkeypatch.setattr(router, "handle_message", agent)
    curator = Curator(On(), session_factory(db_session))
    wakes = []
    curator.use_outbox(job_queue.stage, lambda: wakes.append(1))
    set_curator(curator)
    try:
        await assistant.respond(db_session, test_user.id, "Bola usual na 2 bags rice", "web")
    finally:
        set_curator(None)

    [job] = await jobs(db_session)
    assert job.kind == "curate" and job.payload["message"] == "Bola usual na 2 bags rice"
    assert job.payload["turn_id"] and job.payload["reply"] == "OK." and wakes == [1]


# ── running them ─────────────────────────────────────────────────────────────

async def test_jobs_run_in_order_per_user_and_are_deleted_when_done(db_session, test_user):
    ran = []

    async def record(payload, user_id):
        ran.append((user_id, payload["n"]))

    for n in (1, 2, 3):
        job_queue.stage(db_session, "work", test_user.id, {"n": n})
    job_queue.stage(db_session, "work", 999, {"n": 1})
    await db_session.commit()

    await worker_for(db_session, {"work": record}).run_until_idle()
    assert [n for u, n in ran if u == test_user.id] == [1, 2, 3] and (999, 1) in ran
    assert await jobs(db_session) == []


async def test_a_failing_job_backs_off_and_holds_back_only_its_users_later_jobs(db_session, test_user):
    ran = []
    flaky = {"left": 1}

    async def work(payload, user_id):
        if payload["n"] == 1 and user_id == test_user.id and flaky["left"]:
            flaky["left"] -= 1
            raise RetryLater("graph down")
        ran.append((user_id, payload["n"]))

    for n in (1, 2):
        job_queue.stage(db_session, "work", test_user.id, {"n": n})
    job_queue.stage(db_session, "work", 999, {"n": 1})
    await db_session.commit()
    worker = worker_for(db_session, {"work": work})

    await worker.run_until_idle()
    assert ran == [(999, 1)]  # the other user went ahead; this user's job 2 waits behind job 1
    first = (await jobs(db_session))[0]
    assert first.attempts == 1 and first.last_error == "RetryLater: graph down"
    assert first.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    await db_session.execute(update(Job).values(run_after=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db_session.commit()
    await worker.run_until_idle()
    assert ran == [(999, 1), (test_user.id, 1), (test_user.id, 2)] and await jobs(db_session) == []


async def test_after_too_many_attempts_a_job_is_parked_and_the_queue_moves_on(db_session, test_user):
    ran = []

    async def work(payload, user_id):
        if payload["n"] == 1:
            raise RuntimeError("bug")
        ran.append(payload["n"])

    for n in (1, 2):
        job_queue.stage(db_session, "work", test_user.id, {"n": n})
    await db_session.commit()
    worker = worker_for(db_session, {"work": work})
    for _ in range(MAX_ATTEMPTS):
        await worker.run_until_idle()
        await db_session.execute(update(Job).values(run_after=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db_session.commit()
    await worker.run_until_idle()

    [parked] = await jobs(db_session)
    assert parked.status == "failed" and parked.attempts == MAX_ATTEMPTS and ran == [2]
    assert await JobQueue(session_factory(db_session)).retry_failed() == 1
    assert (await jobs(db_session))[0].status == "pending"


async def test_an_unknown_kind_fails_at_once(db_session, test_user):
    job_queue.stage(db_session, "mystery", test_user.id, {})
    await db_session.commit()
    await worker_for(db_session, {}).run_until_idle()
    [job] = await jobs(db_session)
    assert job.status == "failed" and job.attempts == 1 and "no handler" in job.last_error


async def test_a_job_left_by_a_crashed_worker_is_picked_up(db_session, test_user):
    """A worker that died mid-job never deleted it: it's still pending, and runs again."""
    ran = []
    job_queue.stage(db_session, "work", test_user.id, {"n": 1})
    await db_session.commit()

    async def crash(payload, user_id):
        raise asyncio.CancelledError  # the process going away mid-job

    with pytest.raises(asyncio.CancelledError):
        await worker_for(db_session, {"work": crash}).run_until_idle()
    assert (await jobs(db_session))[0].status == "pending"

    async def work(payload, user_id):
        ran.append(payload["n"])

    await worker_for(db_session, {"work": work}).run_until_idle()
    assert ran == [1] and await jobs(db_session) == []


async def test_the_worker_starts_wakes_and_stops(tmp_path):
    """The running worker, with its own sessions as in the app (not the shared test session)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'jobs.sqlite3').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Model.metadata.create_all(c, tables=[Job.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with maker() as s:
            yield s

    done = asyncio.Event()

    async def work(payload, user_id):
        done.set()

    worker = JobWorker(JobQueue(sessions), {"work": work}, engine, poll_seconds=30)
    await worker.start()
    try:
        await asyncio.sleep(0.1)  # let it find nothing and settle into waiting
        async with maker() as db:
            job_queue.stage(db, "work", 1, {})
            await db.commit()
        worker.wake()  # without this it would wait the full 30 s poll
        await asyncio.wait_for(done.wait(), 15)  # well under the 30 s poll, with slack for a slow machine
    finally:
        await worker.stop()
        await engine.dispose()


# ── graph sync and curation through the queue ────────────────────────────────

async def test_graph_sync_job_end_to_end(db_session, test_user, outbox):
    from memory.sync import GraphSync

    synced = []

    class Store:
        enabled = True

        async def sync_parties(self, user_id, parties):
            synced.append(sorted(p.key for p in parties))

    await persist_transactions(db_session, [sale("Emeka")], test_user.id)
    sync = GraphSync(Store(), session_factory(db_session))
    await worker_for(db_session, {"ledger_changed": sync.handle_job}).run_until_idle()
    assert synced == [["emeka"]] and await jobs(db_session) == []


async def test_a_graph_outage_makes_the_sync_job_retry(db_session, test_user, outbox):
    from memory.models import GraphMemoryError
    from memory.sync import GraphSync

    class Down:
        enabled = True

        async def sync_parties(self, user_id, parties):
            raise GraphMemoryError("timeout")

    await persist_transactions(db_session, [sale("Emeka")], test_user.id)
    await worker_for(db_session, {"ledger_changed": GraphSync(Down(), session_factory(db_session)).handle_job}
                     ).run_until_idle()
    [job] = await jobs(db_session)
    assert job.status == "pending" and job.attempts == 1 and "GraphMemoryError" in job.last_error


# ── two processes, real Postgres ─────────────────────────────────────────────

PG_URL = os.getenv("TEST_JOBS_POSTGRES_URL")


@pytest.mark.skipif(not PG_URL, reason="set TEST_JOBS_POSTGRES_URL to test workers across processes")
async def test_two_processes_never_run_the_same_users_jobs_at_once():
    engines = [create_async_engine(PG_URL), create_async_engine(PG_URL)]
    async with engines[0].begin() as conn:
        await conn.run_sync(lambda c: Model.metadata.create_all(c, tables=[Job.__table__]))
    users = (990101, 990102)
    makers = [async_sessionmaker(e, expire_on_commit=False) for e in engines]
    async with makers[0]() as db:
        await db.execute(Job.__table__.delete().where(Job.user_id.in_(users)))
        for uid in users:
            for n in range(5):
                job_queue.stage(db, "work", uid, {"n": n})
        await db.commit()

    running: dict[int, int] = {uid: 0 for uid in users}
    overlap, ran = [], []

    async def work(payload, user_id):
        running[user_id] += 1
        if running[user_id] > 1:
            overlap.append(user_id)
        await asyncio.sleep(0.05)
        ran.append((user_id, payload["n"]))
        running[user_id] -= 1

    def factory(maker):
        @asynccontextmanager
        async def f():
            async with maker() as s:
                yield s
        return f

    workers = [JobWorker(JobQueue(factory(m)), {"work": work}, e) for m, e in zip(makers, engines)]
    try:
        await asyncio.gather(*(w.run_until_idle() for w in workers))
        assert overlap == []
        for uid in users:
            assert [n for u, n in ran if u == uid] == list(range(5))  # in order, each once
        async with makers[0]() as db:
            assert (await db.execute(select(func.count()).select_from(Job).where(Job.user_id.in_(users)))).scalar() == 0
    finally:
        for e in engines:
            await e.dispose()

"""Workers that run queued jobs (jobs/queue.py).

Each worker loop picks a user with due work, takes a Postgres advisory lock on that user
(so no two workers, in any process, run the same user's jobs), and runs the user's jobs
oldest first until the queue is empty or a job fails. The lock is session-level on the
worker's own connection: if the process dies, Postgres releases it and the user's jobs,
still pending, are picked up elsewhere.

Workers start with the app (main.py lifespan, JOB_WORKERS per process) and are woken the
moment a job is committed in their process; other processes notice within the poll
interval. scripts/jobs.py can run them standalone. On SQLite there are no advisory locks,
so a single worker runs.
"""
import asyncio
import time
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from jobs.queue import JobQueue, Permanent, RetryLater
from utils import logger

JOB_LOCK_NAMESPACE = 0x4A42  # "JB": apart from the per-turn locks (tools/turn_lock.py)

Handler = Callable[[dict, int], Awaitable[None]]  # (payload, user_id)


class JobWorker:
    def __init__(self, queue: JobQueue, handlers: dict[str, Handler], engine: AsyncEngine,
                 concurrency: int = 1, poll_seconds: float = 2.0, expected_errors: tuple = ()):
        """`expected_errors`: failures that are part of life (the graph store being down):
        retried and logged without a traceback."""
        self._queue = queue
        self._handlers = handlers
        self._engine = engine
        self._postgres = engine.dialect.name == "postgresql"
        self._concurrency = max(1, concurrency) if self._postgres else 1
        self._poll = poll_seconds
        self._expected = (RetryLater, Permanent, *expected_errors)
        self._wake = asyncio.Event()
        self._stopping = False
        self._tasks: list[asyncio.Task] = []

    # ── lifecycle ───────────────────────────────────────────────────────────

    def wake(self) -> None:
        """A job was just committed in this process: look now rather than at the next poll."""
        self._wake.set()

    async def start(self) -> None:
        self._stopping = False
        self._tasks = [asyncio.create_task(self._loop(i), name=f"job-worker-{i}") for i in range(self._concurrency)]
        logger.info("jobs.workers_started", workers=self._concurrency, postgres=self._postgres)

    async def stop(self, timeout: float = 20) -> None:
        """Finish the jobs in hand, then stop. Anything unfinished stays queued."""
        self._stopping = True
        self._wake.set()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=timeout)
            for t in pending:
                t.cancel()
        logger.info("jobs.workers_stopped")

    async def run_until_idle(self) -> int:
        """Run due jobs until there are none (scripts and tests). Returns how many users had work."""
        users = 0
        async with self._lock_connection() as conn:
            while await self._pass(conn):
                users += 1
        return users

    # ── the loop ────────────────────────────────────────────────────────────

    async def _loop(self, index: int) -> None:
        while not self._stopping:
            try:
                async with self._lock_connection() as conn:
                    while not self._stopping:
                        # clear *before* looking: a job committed while we look still wakes us
                        self._wake.clear()
                        if not await self._pass(conn):
                            try:
                                await asyncio.wait_for(self._wake.wait(), self._poll)
                            except (asyncio.TimeoutError, TimeoutError):
                                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                # the database went away, a lock connection broke…: back off, then reconnect
                logger.error("jobs.worker_error", worker=index, exc_info=True)
                await asyncio.sleep(min(self._poll * 5, 30))

    def _lock_connection(self):
        engine = self._engine

        class _Conn:
            async def __aenter__(inner):
                inner.conn = await engine.connect() if self._postgres else None
                return inner.conn

            async def __aexit__(inner, *exc):
                if inner.conn is not None:
                    await inner.conn.close()

        return _Conn()

    async def _pass(self, conn) -> bool:
        """Work through one user's due jobs. False if no user had any (or all were taken)."""
        for user_id in await self._queue.due_users():
            if self._postgres and not await self._try_lock(conn, user_id):
                continue  # another worker has this user
            try:
                await self._run_user(user_id)
            finally:
                if self._postgres:
                    await self._unlock(conn, user_id)
            return True
        return False

    async def _try_lock(self, conn, user_id: int) -> bool:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                                  {"ns": JOB_LOCK_NAMESPACE, "uid": int(user_id)})).scalar()
        await conn.commit()
        return bool(got)

    async def _unlock(self, conn, user_id: int) -> None:
        await conn.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"), {"ns": JOB_LOCK_NAMESPACE, "uid": int(user_id)})
        await conn.commit()

    async def _run_user(self, user_id: int) -> None:
        while not self._stopping:
            job = await self._queue.next_job(user_id)
            if job is None:
                return
            handler = self._handlers.get(job["kind"])
            started = time.perf_counter()
            try:
                if handler is None:
                    raise Permanent(f"no handler for job kind {job['kind']!r}")
                await handler(job["payload"], job["user_id"])
            except Exception as e:
                attempts = job["attempts"] + 1
                outcome = await self._queue.fail(job["id"], attempts, f"{type(e).__name__}: {e}",
                                                 permanent=isinstance(e, Permanent))
                log = logger.error if outcome == "failed" else logger.warning
                log("jobs." + ("failed" if outcome == "failed" else "retry"), job_id=job["id"], kind=job["kind"],
                    user_id=user_id, attempts=attempts, error=type(e).__name__, detail=str(e)[:200],
                    exc_info=not isinstance(e, self._expected))
                return  # the user's later jobs wait behind this one (or move on, if it failed for good)
            await self._queue.finish(job["id"])
            logger.info("jobs.done", job_id=job["id"], kind=job["kind"], user_id=user_id, attempts=job["attempts"] + 1,
                        queued_s=job["age_s"], ms=round((time.perf_counter() - started) * 1000, 1))

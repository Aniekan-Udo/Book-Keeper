"""The job queue: a Postgres table used as a transactional outbox.

`stage()` adds a job to the caller's session, so it commits, or rolls back, together with
the change that caused it: a ledger entry and its graph sync, a saved reply and its
curation. Nothing is lost between "committed" and "queued", and nothing is queued for a
change that rolled back.

Jobs belong to a user and run in order per user (graph sync before the curation that
depends on it). A failed job is retried with backoff and holds the user's later jobs
behind it; after JOB_MAX_ATTEMPTS it's marked failed (kept for inspection, see
scripts/jobs.py) and the user's queue moves on. Done jobs are deleted.

Delivery is at-least-once: a worker that dies mid-job leaves it pending for the next one.
Handlers are idempotent (graph sync reconciles; the curator checks before writing).
"""
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db import Job
from memory.sync import SessionFactory

MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "6"))
BACKOFF_SECONDS = (10, 30, 120, 600, 1800)  # after attempt 1, 2, 3, …; the last repeats
ERROR_LIMIT = 500


class RetryLater(Exception):
    """A handler asking to be tried again later (the graph store was down, say)."""


class Permanent(Exception):
    """A job that can never succeed (unknown kind, bad payload): failed at once, no retries."""


def stage(db: AsyncSession, kind: str, user_id: int, payload: dict) -> None:
    """Queue a job in this session: it's saved by the caller's commit, or not at all."""
    db.add(Job(kind=kind, user_id=int(user_id), payload=payload, status="pending", attempts=0,
               run_after=datetime.now(timezone.utc)))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1])


class JobQueue:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    async def due_users(self, limit: int = 20) -> list[int]:
        """Users whose oldest pending job is due, oldest first. A user whose oldest job is
        backing off isn't due: their later jobs wait behind it."""
        async with self._session_factory() as db:
            oldest = select(func.min(Job.id)).where(Job.status == "pending").group_by(Job.user_id)
            rows = (await db.execute(
                select(Job.user_id, Job.run_after).where(Job.id.in_(oldest)).order_by(Job.id).limit(limit * 3)
            )).all()
        now = datetime.now(timezone.utc)
        return [uid for uid, run_after in rows if _aware(run_after) <= now][:limit]

    async def next_job(self, user_id: int) -> dict | None:
        """The user's oldest pending job, if it's due (else None: it's backing off)."""
        async with self._session_factory() as db:
            job = (await db.execute(
                select(Job).where(Job.user_id == user_id, Job.status == "pending").order_by(Job.id).limit(1)
            )).scalar_one_or_none()
            if job is None or _aware(job.run_after) > datetime.now(timezone.utc):
                return None
            return {"id": job.id, "kind": job.kind, "user_id": job.user_id, "payload": job.payload,
                    "attempts": job.attempts, "age_s": round(time.time() - _aware(job.created_at).timestamp(), 1)}

    async def finish(self, job_id: int) -> None:
        async with self._session_factory() as db:
            await db.execute(delete(Job).where(Job.id == job_id))
            await db.commit()

    async def fail(self, job_id: int, attempts: int, error: str, permanent: bool = False) -> str:
        """Record a failed attempt. Returns "retry" or "failed"."""
        gave_up = permanent or attempts >= MAX_ATTEMPTS
        values = {"attempts": attempts, "last_error": error[:ERROR_LIMIT]}
        if gave_up:
            values["status"] = "failed"
        else:
            values["run_after"] = datetime.now(timezone.utc) + backoff(attempts)
        async with self._session_factory() as db:
            await db.execute(update(Job).where(Job.id == job_id).values(**values))
            await db.commit()
        return "failed" if gave_up else "retry"

    async def counts(self) -> dict:
        async with self._session_factory() as db:
            rows = (await db.execute(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status))).all()
        return {f"{kind}:{status}": n for kind, status, n in rows}

    async def failed(self, limit: int = 50) -> list[dict]:
        async with self._session_factory() as db:
            jobs = (await db.execute(
                select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(limit)
            )).scalars().all()
        return [{"id": j.id, "kind": j.kind, "user_id": j.user_id, "attempts": j.attempts,
                 "last_error": j.last_error, "created_at": _aware(j.created_at).isoformat()} for j in jobs]

    async def retry_failed(self, job_ids: list[int] | None = None) -> int:
        """Put failed jobs back in the queue (all of them, or these ids)."""
        q = update(Job).where(Job.status == "failed")
        if job_ids:
            q = q.where(Job.id.in_(job_ids))
        async with self._session_factory() as db:
            result = await db.execute(q.values(status="pending", attempts=0, run_after=datetime.now(timezone.utc)))
            await db.commit()
        return result.rowcount or 0

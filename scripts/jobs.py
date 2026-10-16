"""Look after the job queue (jobs/queue.py).

    uv run python -m scripts.jobs status            pending and failed jobs, by kind
    uv run python -m scripts.jobs failed            the failed jobs, with their last error
    uv run python -m scripts.jobs retry [--id 12]   put failed jobs (all, or these) back in the queue
    uv run python -m scripts.jobs run [--forever]   run workers here: until the queue is empty, or
                                                    as a standalone worker process

The app runs its own workers (JOB_WORKERS per process), so `run` is only needed to drain a
queue while the app is down, or to run workers on a separate machine.
"""
import argparse
import asyncio
import json
import os
import sys

from db import engine, get_db
from jobs.queue import JobQueue
from jobs.worker import JobWorker


async def workers() -> tuple[JobWorker, object]:
    from llm import get_curator_model
    from memory import build_graph_memory
    from memory.curator import Curator
    from memory.models import GraphMemoryError
    from memory.sync import GraphSync

    memory = build_graph_memory()
    curator = Curator(memory, get_db, model_factory=get_curator_model)
    worker = JobWorker(JobQueue(get_db), {"ledger_changed": GraphSync(memory, get_db).handle_job,
                                          "curate": curator.handle_job},
                       engine, concurrency=int(os.getenv("JOB_WORKERS", "2")), expected_errors=(GraphMemoryError,))
    return worker, memory


async def main(args) -> int:
    queue = JobQueue(get_db)
    try:
        if args.command == "status":
            counts = await queue.counts()
            print(json.dumps(counts, indent=2) if counts else "The queue is empty.")
        elif args.command == "failed":
            for job in await queue.failed():
                print(json.dumps(job))
        elif args.command == "retry":
            print(f"{await queue.retry_failed(args.id)} job(s) back in the queue.")
        elif args.command == "run":
            worker, memory = await workers()
            try:
                if args.forever:
                    await worker.start()
                    await asyncio.Event().wait()  # until interrupted
                else:
                    print(f"Done: work for {await worker.run_until_idle()} user(s).")
            finally:
                await worker.stop()
                await memory.close()
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("failed")
    retry = sub.add_parser("retry")
    retry.add_argument("--id", type=int, action="append", help="job id; repeat for several (default: all failed)")
    run = sub.add_parser("run")
    run.add_argument("--forever", action="store_true", help="keep running as a worker process")
    try:
        sys.exit(asyncio.run(main(parser.parse_args())))
    except KeyboardInterrupt:
        sys.exit(0)

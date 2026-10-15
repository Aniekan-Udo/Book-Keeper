"""The Collector's daily run: settle every overdue promise (broken, or kept if it was in
fact paid on time) for every trader.

    uv run python -m scripts.collector_sweep            all traders
    uv run python -m scripts.collector_sweep --user 12

Run it once a day (cron, a scheduled container, …). It's also done automatically whenever
a trader asks who to chase, so this only keeps promise history current for traders who
haven't asked. Idempotent: safe to run as often as you like.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from db import User, engine, get_db
from memory import build_graph_memory
from memory.collector import Collector
from memory.models import GraphMemoryError
from utils import logger


async def main(user_ids: list[int] | None) -> int:
    memory = build_graph_memory()
    if not memory.enabled or not await memory.ping():
        print("Graph memory is off or FalkorDB isn't reachable — nothing to sweep.")
        await memory.close()
        return 1
    collector = Collector(memory, get_db)
    failed = 0
    try:
        if user_ids is None:
            async with get_db() as db:
                user_ids = list((await db.execute(select(User.id).order_by(User.id))).scalars().all())
        for user_id in user_ids:
            try:
                counts = await collector.sweep(user_id)
                print(f"trader {user_id}: {counts['broken']} broken, {counts['kept']} kept")
            except GraphMemoryError as e:
                failed += 1
                logger.error("collector_sweep.failed", user_id=user_id, error=str(e))
                print(f"trader {user_id}: FAILED ({e}) — safe to run again")
    finally:
        await memory.close()
        await engine.dispose()
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", type=int, action="append", help="trader (user) id; repeat for several")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.user)))

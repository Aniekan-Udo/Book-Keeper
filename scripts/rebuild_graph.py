"""Bring the graph back in line with the Postgres ledger.

    uv run python -m scripts.rebuild_graph --user 12      one trader
    uv run python -m scripts.rebuild_graph --all          every trader
    uv run python -m scripts.rebuild_graph --user 12 --wipe

Safe to run any time, as often as you like: it resyncs parties, debts and inferred
customer roles, removes debts the ledger no longer has, and restores confirmed bank names
(kept in Postgres, party_aliases) the graph is missing. What the agents learned
(aliases, promises, facts, stated roles) is kept, because it can't be rebuilt from Postgres.

--wipe deletes the trader's whole graph first, learned memory included. Use it only to
start a trader's memory from scratch.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from db import User, engine, get_db
from memory import build_graph_memory
from memory.models import GraphMemoryError
from memory.sync import GraphSync
from utils import logger


async def main(user_ids: list[int] | None, wipe: bool) -> int:
    memory = build_graph_memory()
    if not memory.enabled:
        print("GRAPH_MEMORY is off — nothing to rebuild.")
        return 1
    if not await memory.ping():
        print("FalkorDB isn't reachable. Is it running (docker compose up falkordb)?")
        return 1

    sync = GraphSync(memory, get_db)
    failed = 0
    try:
        if user_ids is None:
            async with get_db() as db:
                user_ids = list((await db.execute(select(User.id).order_by(User.id))).scalars().all())
        for user_id in user_ids:
            try:
                if wipe:
                    await memory.delete_trader_graph(user_id)
                summary = await sync.rebuild(user_id)
                print(f"trader {user_id}: {summary['parties']} parties, {summary['debts']} debts, "
                      f"{summary['removed']} stale removed, {summary['bank_names']} bank names restored")
            except GraphMemoryError as e:
                failed += 1
                logger.error("rebuild_graph.failed", user_id=user_id, error=str(e))
                print(f"trader {user_id}: FAILED ({e}) — safe to run again")
    finally:
        await memory.close()
        await engine.dispose()
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", type=int, action="append", help="trader (user) id; repeat for several")
    who.add_argument("--all", action="store_true", help="every trader")
    parser.add_argument("--wipe", action="store_true", help="delete the trader's graph first (loses learned memory)")
    args = parser.parse_args()
    if args.wipe and args.all:
        parser.error("--wipe with --all would erase every trader's learned memory; name traders with --user")
    sys.exit(asyncio.run(main(None if args.all else args.user, args.wipe)))

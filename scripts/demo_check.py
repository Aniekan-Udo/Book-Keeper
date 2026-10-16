"""Rehearse the demo: send the same messages to the day-30 and day-1 accounts (made by
scripts/seed_demo.py) through the real assistant and model, and print the replies side
by side.

    uv run python -m scripts.demo_check
    uv run python -m scripts.demo_check "who I suppose chase?" "why Emeka first?"

This records real entries (e.g. Mama Chidi's payment) in both demo accounts, so re-seed
before recording: `uv run python -m scripts.seed_demo --reset`.

Uses real model calls: the six messages on both accounts took about 32,000 tokens. On
Groq's free tier (200,000 tokens/day, plus a per-minute limit) that's fine; DEMO_PAUSE
(default 15 s) spaces the runs out for the per-minute limit.
"""
import asyncio
import os
import sys
import textwrap

from sqlalchemy import select

from db import User, engine, get_db
from main import app, lifespan
from memory import get_curator
from scripts.seed_demo import DAY1, DAY30
from tools.assistant import respond
from tools.ledger_events import ledger_events

DEMO = [
    "who I suppose chase?",
    "why Emeka first?",
    "Bola come, I give am the usual",
    # a bank name that shares no word with "Mama Chidi": only memory can match it
    "(forwarded alert) Credit Alert! Amt: NGN30,000.00 Desc: TRF FROM OKAFOR CHINYERE MARY",
    "Chidi's mum take 1 bag rice, 45k, she go pay Monday",
    "remind Pastor John",
]
WIDTH = 58
# seconds between model runs: Groq's free tier also limits tokens per minute
PAUSE = float(os.getenv("DEMO_PAUSE", "15"))


def columns(left: str, right: str) -> str:
    a = textwrap.wrap(left, WIDTH) or [""]
    b = textwrap.wrap(right, WIDTH) or [""]
    a += [""] * (len(b) - len(a))
    b += [""] * (len(a) - len(b))
    return "\n".join(f"{x:<{WIDTH}} │ {y}" for x, y in zip(a, b))


async def ask(user_id: int, message: str) -> str:
    async with get_db() as db:
        try:
            reply = (await respond(db, user_id, message, channel="whatsapp")).reply
        except Exception as e:  # show the failure in the column, keep rehearsing
            reply = f"[{type(e).__name__}: {e}]"
    await get_curator().drain()
    await ledger_events.drain()
    return " ".join(reply.split())


async def main(messages: list[str]) -> int:
    async with get_db() as db:
        ids = {email: (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
               for email, _ in (DAY30, DAY1)}
    if None in ids.values():
        print("Demo accounts not found. Run: uv run python -m scripts.seed_demo")
        return 1
    async with lifespan(app):
        print(columns("DAY 30 (a month of graph memory)", "DAY 1 (same ledger, nothing learned)"))
        print("─" * (WIDTH * 2 + 3))
        for message in messages:
            print(f"\n>>> {message}\n")
            # one after the other: kinder to rate limits, and SQLite allows one writer at a time
            day30 = await ask(ids[DAY30[0]], message)
            await asyncio.sleep(PAUSE)
            day1 = await ask(ids[DAY1[0]], message)
            await asyncio.sleep(PAUSE)
            print(columns(day30, day1))
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or DEMO)))

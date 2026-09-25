"""Pilot review report: how each trader is using the bot, and every moment it likely got
something wrong — so those exchanges can be fixed and turned into eval cases.

    uv run python -m tools.pilot_report                 # last 7 days, to the screen
    uv run python -m tools.pilot_report --days 14 --out ~/pilot-week2.md

Run it where the app's database is (POSTGRES_URI), e.g. on the server:
    docker compose exec api uv run python -m tools.pilot_report --out /tmp/report.md

The report contains traders' own messages and business figures. Keep it private, don't
commit it, and delete it once reviewed.
"""
import argparse
import asyncio
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import BankAlert, ConversationTurn, Reminder, SessionLocal, TransactionORM, User, engine
from tools.assistant import FALLBACK_REPLY

# a trader telling the bot it got something wrong: "cancel that", "no be 50k", "wrong", ...
CORRECTION = re.compile(
    r"\b(cancel|undo|delete|remove|wrong|mistake|not\s+(right|correct|\d)|no\s+be|sorry|i\s+mean|"
    r"that'?s\s+not|isn'?t|ehn?\s+no|abeg\s+(change|remove))\b",
    re.IGNORECASE,
)
# replies that mean the bot couldn't help
GAVE_UP = ("wasn't able to process", "something went wrong", "temporarily unavailable", "a bit busy",
           "couldn't process", "couldn't read", "couldn't hear")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_correction(text: str) -> bool:
    return bool(CORRECTION.search(text))


def gave_up(reply: str) -> bool:
    lowered = reply.lower()
    return reply == FALLBACK_REPLY or any(p in lowered for p in GAVE_UP)


def _quote(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


async def build_report(db: AsyncSession, days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    users = {u.id: u for u in (await db.execute(select(User))).scalars().all()}

    turns = (await db.execute(
        select(ConversationTurn).where(ConversationTurn.created_at >= since)
        .order_by(ConversationTurn.user_id, ConversationTurn.created_at, ConversationTurn.id)
    )).scalars().all()
    entries = (await db.execute(select(TransactionORM).where(TransactionORM.created_at >= since))).scalars().all()
    alerts = (await db.execute(select(BankAlert).where(BankAlert.created_at >= since))).scalars().all()
    reminders = (await db.execute(select(Reminder).where(Reminder.created_at >= since))).scalars().all()

    by_user: dict[int, list[ConversationTurn]] = defaultdict(list)
    for t in turns:
        by_user[t.user_id].append(t)

    stats: dict[int, Counter] = defaultdict(Counter)
    for e in entries:
        stats[e.user_id]["entries"] += 1
        stats[e.user_id]["undone"] += e.is_deleted
        stats[e.user_id]["missing amount"] += (e.status == "needs_amount" and not e.is_deleted)
    for a in alerts:
        stats[a.user_id]["alerts"] += 1
        stats[a.user_id]["alerts unanswered"] += a.status == "pending"
    for r in reminders:
        stats[r.user_id]["reminders sent"] += r.status == "sent"

    review: list[str] = []
    for user_id, user_turns in by_user.items():
        s = stats[user_id]
        s["active days"] = len({_aware(t.created_at).date() for t in user_turns if t.role == "user"})
        for i, t in enumerate(user_turns):
            if t.role == "user":
                s["messages"] += 1
                s["voice notes"] += t.content.startswith("(voice note)")
                s["screenshots"] += t.content.startswith("(forwarded alert screenshot)")
                s["whatsapp"] += t.channel == "whatsapp"
            prev = user_turns[i - 2:i]  # the exchange before this turn
            if t.role == "user" and is_correction(t.content) and len(prev) == 2:
                s["corrections"] += 1
                review.append(_exchange(users.get(user_id), "Trader corrected the bot", prev + [t]))
            if t.role == "assistant" and gave_up(t.content):
                s["gave up"] += 1
                review.append(_exchange(users.get(user_id), "Bot couldn't handle it", user_turns[max(0, i - 1):i + 1]))

    lines = [f"# Pilot report — last {days} days", "",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Contains traders' messages: keep private.", "",
             "## Traders", "",
             "| Trader | Active days | Messages | WhatsApp | Voice | Screenshots | Entries | Undone | Corrections | Bot gave up | Missing amount | Alerts (unanswered) | Reminders sent |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for user_id in sorted(stats, key=lambda u: -stats[u]["messages"]):
        s, u = stats[user_id], users.get(user_id)
        name = f"{u.business_name or ''} ({u.firstname})" if u else f"user {user_id}"
        lines.append(f"| {name} | {s['active days']} | {s['messages']} | {s['whatsapp']} | {s['voice notes']} | "
                     f"{s['screenshots']} | {s['entries']} | {s['undone']} | {s['corrections']} | {s['gave up']} | "
                     f"{s['missing amount']} | {s['alerts']} ({s['alerts unanswered']}) | {s['reminders sent']} |")
    if not stats:
        lines.append("| (no activity) |||||||||||||")

    total = Counter()
    for s in stats.values():
        total.update(s)
    lines += ["", "## Overall", "",
              f"- Messages: {total['messages']} ({total['voice notes']} voice notes, {total['screenshots']} screenshots)",
              f"- Entries recorded: {total['entries']}, of which undone: {total['undone']}",
              f"- Corrections: {total['corrections']} · Bot gave up: {total['gave up']}"]
    if total["messages"]:
        lines.append(f"- Messages needing a fix (corrections + gave up): "
                     f"{(total['corrections'] + total['gave up']) / total['messages']:.0%}")

    lines += ["", f"## To review ({len(review)})", "",
              "Each of these is a likely mistake. Decide what the right result was, fix the prompt if needed, "
              "and add the message to `tools/evals.py` as a case.", ""]
    lines += review or ["Nothing flagged."]
    return "\n".join(lines) + "\n"


def _exchange(user: User | None, why: str, turns: list[ConversationTurn]) -> str:
    who = f"{user.business_name} ({user.firstname})" if user else "unknown trader"
    when = _aware(turns[-1].created_at).strftime("%b %d %H:%M")
    body = "\n".join(f"> **{'Trader' if t.role == 'user' else 'Bot'}:** {_quote(t.content)}" for t in turns)
    return f"### {why} — {who}, {when}\n\n{body}\n"


async def _main(days: int, out: str | None) -> None:
    async with SessionLocal() as db:
        report = await build_report(db, days)
    await engine.dispose()
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Wrote {out}")
    else:
        print(report)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", help="write the report to this file instead of the screen")
    args = parser.parse_args()
    asyncio.run(_main(args.days, args.out))

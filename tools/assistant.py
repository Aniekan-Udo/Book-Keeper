"""One conversation turn, the same for every channel (web chat, WhatsApp).

Loads the user's recent history and any entries still waiting for an amount, runs the
agent, and remembers the exchange — so a follow-up like "15k" or "cancel that" makes sense.
"""
import weakref
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import ConversationTurn, TransactionORM
from tools.clean_version import build_tools
from tools.debt_collection import pending_drafts
from tools.prompts import router_sys_msg
from tools import router

HISTORY_WINDOW = timedelta(minutes=30)  # older than this, a new message starts a new conversation
HISTORY_TURNS = 8
PENDING_LIMIT = 5
FALLBACK_REPLY = "I wasn't able to process that. Could you rephrase or try again?"

# one turn at a time per user, so two quick messages can't interleave their history
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


@dataclass
class AssistantReply:
    reply: str
    clarifications: list[dict] = field(default_factory=list)


async def load_history(db: AsyncSession, user_id: int) -> list[dict]:
    since = datetime.now(timezone.utc) - HISTORY_WINDOW
    turns = (await db.execute(
        select(ConversationTurn)
        .where(ConversationTurn.user_id == user_id, ConversationTurn.created_at >= since)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
        .limit(HISTORY_TURNS)
    )).scalars().all()
    return [{"role": t.role, "content": t.content} for t in reversed(turns)]


async def pending_amounts(db: AsyncSession, user_id: int) -> list[str]:
    rows = (await db.execute(
        select(TransactionORM)
        .where(TransactionORM.user_id == user_id, TransactionORM.status == "needs_amount",
               TransactionORM.is_deleted.is_(False))
        .order_by(TransactionORM.created_at.desc(), TransactionORM.id.desc())
        .limit(PENDING_LIMIT)
    )).scalars().all()
    lines = []
    for r in rows:
        what = r.item or r.type.replace("_", " ")
        who = f" ({r.party})" if r.party else ""
        lines.append(f"#{r.id}: {r.type} — {what}{who}, {r.date.isoformat()}")
    return lines


async def respond(db: AsyncSession, user_id: int, text: str, channel: str) -> AssistantReply:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = _user_locks[user_id] = asyncio.Lock()

    async with lock:
        started = datetime.now(timezone.utc)
        history = await load_history(db, user_id)
        pending = await pending_amounts(db, user_id)
        drafts = await pending_drafts(db, user_id)

        clarifications: list[dict] = []
        tools = build_tools(db, user_id, clarifications, request_started_at=started)
        prompt = router_sys_msg(channel, pending, drafts)
        result = await router.handle_message(text, prompt, tools, history)
        reply = result["messages"][-1].text or FALLBACK_REPLY

        db.add_all([
            ConversationTurn(user_id=user_id, role="user", content=text, channel=channel),
            ConversationTurn(user_id=user_id, role="assistant", content=reply, channel=channel),
        ])
        await db.commit()
        return AssistantReply(reply=reply, clarifications=clarifications)

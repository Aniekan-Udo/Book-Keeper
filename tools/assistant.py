"""One conversation turn, the same for every channel (web chat, WhatsApp).

    recall (graph memory about the people mentioned)
      → the agent team (agents/team.py: routed to the Recorder, Collector or Alert matcher)
      → save the exchange
      → the memory curator, in the background (memory/curator.py)

Short-term memory is the recent history and entries still waiting for an amount, so a
follow-up like "15k" or "cancel that" makes sense. Long-term memory is the graph.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.team import TurnContext, run_team
from db import ConversationTurn, TransactionORM
from memory import get_curator, get_graph_memory
from memory.curator import Turn
from memory.recall import prompt_section, recall
from observability import message_tracer, trace_id_of
from tools.clean_version import build_tools
from tools.alerts import pending_alerts
from tools.debt_collection import pending_drafts
from tools.turn_lock import user_turn_lock
from utils import logger

HISTORY_WINDOW = timedelta(minutes=30)  # older than this, a new message starts a new conversation
HISTORY_TURNS = 8
PENDING_LIMIT = 5
FALLBACK_REPLY = "I wasn't able to process that. Could you rephrase or try again?"



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
    # one turn at a time per user, in this process and across all others (tools/turn_lock.py)
    async with user_turn_lock(db, user_id):
        started = datetime.now(timezone.utc)
        history = await load_history(db, user_id)
        recalled = await recall(get_graph_memory(), user_id, text)  # never raises

        clarifications: list[dict] = []
        touched: list[dict] = []
        notes: list[dict] = []
        tracer = message_tracer(user_id, channel)
        ctx = TurnContext(
            text=text, channel=channel, history=history, user_id=user_id,
            tools=build_tools(db, user_id, clarifications, request_started_at=started, touched=touched,
                              could_be=recalled.could_be,
                              last_reply=next((h["content"] for h in reversed(history) if h["role"] == "assistant"),
                                              ""),
                              memory_notes=notes),
            pending_amounts=await pending_amounts(db, user_id),
            reminder_drafts=await pending_drafts(db, user_id),
            pending_alerts=await pending_alerts(db, user_id),
            memory=prompt_section(recalled),
            callbacks=[tracer] if tracer else None,
            touched=touched,
        )
        team = await run_team(ctx)
        reply = team.result["messages"][-1].text or FALLBACK_REPLY

        user_turn = ConversationTurn(user_id=user_id, role="user", content=text, channel=channel)
        db.add_all([
            user_turn,
            # the Opik trace of how this reply was produced, for the pilot report to point at
            ConversationTurn(user_id=user_id, role="assistant", content=reply, channel=channel,
                             trace_id=trace_id_of(tracer)),
        ])
        await db.flush()
        turn = Turn(user_id=user_id, turn_id=user_turn.id, message=text, reply=reply, channel=channel,
                    history=history, touched=touched, recalled=recalled, notes=notes)
        curator = get_curator()
        curator.stage(db, turn)      # with the job queue: saved together with the reply
        await db.commit()
        curator.committed(turn)      # in-process: start curating; queue: wake the worker
        logger.debug("assistant.turn", user_id=user_id, agents=team.trail, route=team.route_reason,
                     touched=len(touched), remembered=len(recalled.people))
        return AssistantReply(reply=reply, clarifications=clarifications)

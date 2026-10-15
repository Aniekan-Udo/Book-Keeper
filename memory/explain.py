"""The "why?" tool's work: everything remembered about a person, with where each piece
came from (the trader's own message, and which agent wrote it), ready for an agent to put
into words. "Why Emeka first?" is answered from the graph, not from the model's guess.

Message text isn't stored in the graph (only its conversation_turns id), so it's fetched
from Postgres here.
"""
from sqlalchemy import select

from db import ConversationTurn
from memory.models import GraphMemoryError
from memory.ports import GraphMemory
from memory.sync import SessionFactory
from utils import logger

QUOTE_LIMIT = 160
STATUS_WORDS = {"open": "still open", "kept": "kept", "broken": "broken", "replaced": "replaced by a later date"}


async def resolve_party(memory: GraphMemory, user_id: int, name: str) -> tuple[str, str] | None:
    """(party key, party name) for a name or alias the trader uses, or None."""
    key = " ".join(name.lower().split())
    for known in await memory.known_names(user_id):
        if known.key == key:
            return known.party_key, known.party_name
    return None


async def explain(memory: GraphMemory, session_factory: SessionFactory, user_id: int, name: str) -> dict:
    if not memory.enabled:
        return {"error": "Memory is switched off, so there's no history to explain."}
    try:
        found = await resolve_party(memory, user_id, name)
        if found is None:
            return {"error": f"I don't have anything remembered about {name}."}
        key, display = found
        e = await memory.explain(user_id, key)
    except GraphMemoryError as err:
        logger.warning("explain.failed", user_id=user_id, error=str(err))
        return {"error": "I can't reach my memory right now. Try again in a moment."}

    quotes: dict[int, tuple[str, str]] = {}
    if e.turn_ids():
        async with session_factory() as db:
            rows = (await db.execute(
                select(ConversationTurn.id, ConversationTurn.content, ConversationTurn.created_at)
                .where(ConversationTurn.user_id == user_id, ConversationTurn.id.in_(e.turn_ids()))
            )).all()
        quotes = {i: (c[:QUOTE_LIMIT], at.date().isoformat()) for i, c, at in rows}

    def source(item: dict) -> dict:
        out = {"written_by": item.get("by")}
        if item.get("turn_id") in quotes:
            text, day = quotes[item["turn_id"]]
            out.update(you_said=text, on=day)
        return out

    promises = [{"due": p["due"], "outcome": STATUS_WORDS.get(p["status"], p["status"]),
                 **({"paid": f"{round(p['paid_fraction'] * 100)}%"} if p["status"] == "broken"
                    and p["paid_fraction"] is not None else {}),
                 **({"decided_by": p["closed_by"], "decided_on": p["closed_on"]} if p["closed_by"] else {}),
                 **source(p)} for p in e.promises]
    counted = [p for p in e.promises if p["status"] in ("kept", "broken")]
    result = {
        "person": display,
        "promises": promises,
        "promise_record": (f"kept {sum(p['status'] == 'kept' for p in counted)}, "
                           f"broken {sum(p['status'] == 'broken' for p in counted)}") if counted else "none settled yet",
        "remembered": [{"what": f["kind"].replace("_", " "), "detail": f["summary"],
                        "status": "in force" if not f["ended"] else f"ended {f['ended'][:10]}",
                        "how": "you said so" if f["confidence"] == "stated" else "worked out from your sales",
                        **source(f)} for f in e.facts],
        "also_called": [{"name": a["text"], "kind": a["kind"].replace("_", " "),
                         "status": "current" if not a["ended"] else "no longer", **source(a)} for a in e.aliases],
        "roles": [{"role": r["role"], "status": "current" if not r["ended"] else f"ended {str(r['ended'])[:10]}",
                   "how": "you said so" if r["confidence"] == "stated" else "from the records"} for r in e.roles],
        "reminders_sent": [{"on": str(r["sent_at"])[:10], "because_of_promises": r["because"]} for r in e.reminders],
    }
    logger.info("explain.done", user_id=user_id, promises=len(promises), facts=len(e.facts),
                aliases=len(e.aliases), quotes=len(quotes))
    return result

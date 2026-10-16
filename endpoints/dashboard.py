"""Read-only views for the web app: the books at a glance, who to chase, the people the
business deals with and what's remembered about them, and the conversation so far.

All of it is plain code over Postgres and the graph: no model calls, so these pages cost
no tokens and always show the same thing for the same books. Everything is scoped to
the logged-in user.
"""
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import get_current_user
from config import business_today
from db import ConversationTurn, TransactionORM, User, get_db_session
from memory import get_graph_memory
from memory.collector import Collector
from memory.explain import explain
from memory.models import GraphMemoryError
from utils import logger

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

RECEIVABLE = ("sale", "loan_given")
PAYABLE = ("expense", "loan_taken")
RECENT = 10
HISTORY_TURNS = 40

Period = Literal["month", "last_month", "all"]


def _money(value) -> float:
    return float(value or 0)


def _key(name: str) -> str:
    return name.strip().lower()  # the ledger's own grouping (tools/ledger._party_key)


def _session(db: AsyncSession):
    @asynccontextmanager
    async def factory():
        yield db  # one request, one session: the views run one query at a time
    return factory


def _period(period: Period) -> tuple[date | None, date | None, str]:
    today = business_today()
    first = today.replace(day=1)
    if period == "month":
        return first, today, today.strftime("%B %Y")
    if period == "last_month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end, end.strftime("%B %Y")
    return None, None, "All time"


def _entry(r: TransactionORM) -> dict:
    return {"id": r.id, "date": r.date.isoformat(), "type": r.type, "party": r.party, "item": r.item,
            "quantity": r.quantity, "unit": r.unit, "amount": _money(r.amount) if r.amount is not None else None,
            "outstanding": _money(r.outstanding), "due_date": r.due_date.isoformat() if r.due_date else None,
            "status": r.status}


def _live(user: User):
    return (TransactionORM.user_id == user.id, TransactionORM.is_deleted.is_(False))


@router.get("/overview")
async def overview(period: Period = "month", db: AsyncSession = Depends(get_db_session),
                   user: User = Depends(get_current_user)):
    """Sales, spending and profit for a period; who owes and who is owed, right now."""
    start, end, label = _period(period)

    async def total(txn_type: str) -> Decimal:
        q = select(func.coalesce(func.sum(TransactionORM.amount), 0)).where(
            *_live(user), TransactionORM.type == txn_type, TransactionORM.status == "complete")
        if start:
            q = q.where(TransactionORM.date >= start, TransactionORM.date <= end)
        return (await db.execute(q)).scalar_one()

    async def balances(types: tuple[str, ...]) -> list[dict]:
        rows = (await db.execute(
            select(TransactionORM.party, TransactionORM.outstanding)
            .where(*_live(user), TransactionORM.status == "complete", TransactionORM.outstanding > 0,
                   TransactionORM.type.in_(types))
        )).all()
        grouped: dict[str, dict] = {}
        for party, outstanding in rows:
            name = (party or "").strip() or "Unnamed"
            g = grouped.setdefault(_key(name), {"name": name, "amount": Decimal("0")})
            g["amount"] += outstanding
        return sorted(({"name": g["name"], "amount": _money(g["amount"])} for g in grouped.values()),
                      key=lambda g: -g["amount"])

    sales, spent = await total("sale"), await total("expense")
    owed_to_you, you_owe = await balances(RECEIVABLE), await balances(PAYABLE)
    recent = (await db.execute(
        select(TransactionORM).where(*_live(user))
        .order_by(TransactionORM.date.desc(), TransactionORM.id.desc()).limit(RECENT)
    )).scalars().all()
    waiting = (await db.execute(
        select(func.count()).select_from(TransactionORM)
        .where(*_live(user), TransactionORM.status == "needs_amount")
    )).scalar_one()
    return {
        "period": label,
        "sales": _money(sales), "expenses": _money(spent), "profit": _money(sales - spent),
        "owed_to_you": {"total": sum(g["amount"] for g in owed_to_you), "people": owed_to_you},
        "you_owe": {"total": sum(g["amount"] for g in you_owe), "people": you_owe},
        "waiting_for_amount": waiting,
        "recent": [_entry(r) for r in recent],
    }


@router.get("/chase")
async def chase(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """Who to chase first, and why (the Collector's ranking: no model involved)."""
    return await Collector(get_graph_memory(), _session(db)).who_to_chase(user.id)


@router.get("/people")
async def people(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """Everyone in the books, with balances, and what graph memory holds about them."""
    rows = (await db.execute(
        select(TransactionORM).where(*_live(user), TransactionORM.party.is_not(None))
        .order_by(TransactionORM.date, TransactionORM.id)
    )).scalars().all()
    found: dict[str, dict] = {}
    for r in rows:
        name = r.party.strip()
        if not name:
            continue
        p = found.setdefault(_key(name), {"key": _key(name), "name": name, "owes_you": Decimal("0"),
                                          "you_owe": Decimal("0"), "entries": 0, "last_seen": r.date})
        p["name"], p["last_seen"] = name, max(p["last_seen"], r.date)
        p["entries"] += 1
        if r.status == "complete" and r.outstanding:
            if r.type in RECEIVABLE:
                p["owes_you"] += r.outstanding
            elif r.type in PAYABLE:
                p["you_owe"] += r.outstanding

    memories = {}
    memory = get_graph_memory()
    if memory.enabled and found:
        try:
            memories = {m.key: m for m in await memory.recall(user.id, list(found))}
        except GraphMemoryError as e:
            logger.warning("dashboard.recall_failed", user_id=user.id, error=str(e))

    out = []
    for key, p in found.items():
        m = memories.get(key)
        out.append({
            "name": p["name"], "owes_you": _money(p["owes_you"]), "you_owe": _money(p["you_owe"]),
            "entries": p["entries"], "last_seen": p["last_seen"].isoformat(),
            "roles": list(m.roles) if m else [],
            "also_called": list(m.aliases) if m else [],
            "promises": {"kept": m.kept, "broken": m.broken,
                         "open": sum(pr.status == "open" for pr in m.promises)} if m else None,
            "remembered": [{"kind": f.kind, "summary": f.summary, "said": f.said, "how": f.confidence,
                            "until": f.until} for f in m.facts] if m else [],
        })
    out.sort(key=lambda p: p["last_seen"], reverse=True)
    return {"memory": "on" if memory.enabled else "off", "people": out}


@router.get("/people/{name}")
async def person(name: str, db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """One person: their entries, and everything remembered with where it came from."""
    entries = (await db.execute(
        select(TransactionORM).where(*_live(user), func.lower(func.trim(TransactionORM.party)) == _key(name))
        .order_by(TransactionORM.date.desc(), TransactionORM.id.desc()).limit(30)
    )).scalars().all()
    why = await explain(get_graph_memory(), _session(db), user.id, name)
    if not entries and "error" in why:
        raise HTTPException(status_code=404, detail=f"No one called {name} in your books.")
    return {"name": entries[0].party.strip() if entries else why.get("person", name),
            "entries": [_entry(r) for r in entries], "memory": why}


@router.get("/history")
async def history(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """The latest conversation turns, oldest first, so the chat survives a reload."""
    turns = (await db.execute(
        select(ConversationTurn).where(ConversationTurn.user_id == user.id)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(HISTORY_TURNS)
    )).scalars().all()
    return {"turns": [{"role": t.role, "content": t.content, "channel": t.channel,
                       "at": t.created_at.isoformat()} for t in reversed(turns)]}

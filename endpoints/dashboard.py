"""Read-only views for the web app: the books (earnings, spending and profit by period,
what sold and what the money went on), who owes you and who you owe (for what, how much,
when it's due), the people the business deals with and what's remembered about them,
who to chase, and the conversation so far.

All of it is plain code over Postgres and the graph: no model calls, so these pages cost
no tokens and always show the same thing for the same books. Everything is scoped to
the logged-in user.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import get_current_user
from config import business_today
from db import ConversationTurn, PartyAlias, TransactionORM, User, get_db_session
from memory import get_graph_memory
from memory.collector import Collector
from memory.curator import Curator
from memory.explain import explain, resolve_party
from memory.models import GraphMemoryError, Source
from utils import logger

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

RECEIVABLE = ("sale", "loan_given")
PAYABLE = ("expense", "loan_taken")
RECENT = 10
HISTORY_TURNS = 40

Period = Literal["today", "week", "month", "last_month", "all"]
TOP_ITEMS = 8


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
    if period == "today":
        return today, today, "Today, " + today.strftime("%d %B").lstrip("0")
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        return monday, today, "This week, since Monday " + monday.strftime("%d %B").lstrip("0")
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


def _unit(unit: str | None) -> str:
    return (unit or "").strip().lower().rstrip("s")


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

    async def by_item(txn_type: str) -> list[dict]:
        """What sold (or what the money went on) in the period, biggest first."""
        q = select(TransactionORM.item, TransactionORM.amount, TransactionORM.quantity, TransactionORM.unit).where(
            *_live(user), TransactionORM.type == txn_type, TransactionORM.status == "complete")
        if start:
            q = q.where(TransactionORM.date >= start, TransactionORM.date <= end)
        grouped: dict[str, dict] = {}
        for item, amount, qty, unit in (await db.execute(q)).all():
            name = (item or "").strip() or "Other"
            g = grouped.setdefault(name.lower(), {"item": name, "amount": Decimal("0"), "entries": 0,
                                                  "quantity": 0.0, "unit": unit})
            g["amount"] += amount or 0
            g["entries"] += 1
            if qty and _unit(unit) == _unit(g["unit"]):  # "bag" and "bags" add up
                g["quantity"] += qty
                if unit and len(unit) > len(g["unit"] or ""):
                    g["unit"] = unit  # show the plural
        rows = sorted(grouped.values(), key=lambda g: -g["amount"])[:TOP_ITEMS]
        return [{"item": g["item"], "amount": _money(g["amount"]), "entries": g["entries"],
                 "quantity": g["quantity"] or None, "unit": g["unit"]} for g in rows]

    async def unpaid_sales() -> Decimal:
        q = select(func.coalesce(func.sum(TransactionORM.outstanding), 0)).where(
            *_live(user), TransactionORM.type == "sale", TransactionORM.status == "complete")
        if start:
            q = q.where(TransactionORM.date >= start, TransactionORM.date <= end)
        return (await db.execute(q)).scalar_one()

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
        "sales_on_credit": _money(await unpaid_sales()),
        "sales_by_item": await by_item("sale"), "expenses_by_item": await by_item("expense"),
        "owed_to_you": {"total": sum(g["amount"] for g in owed_to_you), "people": owed_to_you},
        "you_owe": {"total": sum(g["amount"] for g in you_owe), "people": you_owe},
        "waiting_for_amount": waiting,
        "recent": [_entry(r) for r in recent],
    }


@router.get("/debts")
async def debts(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """Who owes you and who you owe, right now: per person, every open entry with what it
    was for, the amount, what's been paid, the balance and the due date."""
    today = business_today()
    rows = (await db.execute(
        select(TransactionORM)
        .where(*_live(user), TransactionORM.status == "complete", TransactionORM.outstanding > 0,
               TransactionORM.type.in_(RECEIVABLE + PAYABLE))
        .order_by(TransactionORM.date, TransactionORM.id)
    )).scalars().all()

    def side(types: tuple[str, ...]) -> dict:
        people: dict[str, dict] = {}
        for r in (r for r in rows if r.type in types):
            name = (r.party or "").strip() or "Unnamed"
            p = people.setdefault(_key(name), {"name": name, "balance": Decimal("0"), "entries": [],
                                               "next_due": None, "overdue": False})
            overdue_days = (today - r.due_date).days if r.due_date and r.due_date < today else 0
            p["balance"] += r.outstanding
            p["entries"].append({
                "id": r.id, "date": r.date.isoformat(), "type": r.type, "item": r.item,
                "quantity": r.quantity, "unit": r.unit, "amount": _money(r.amount),
                "paid": _money((r.amount or 0) - r.outstanding), "balance": _money(r.outstanding),
                "due_date": r.due_date.isoformat() if r.due_date else None, "days_overdue": overdue_days,
            })
            if r.due_date and (p["next_due"] is None or r.due_date.isoformat() < p["next_due"]):
                p["next_due"] = r.due_date.isoformat()
            p["overdue"] = p["overdue"] or overdue_days > 0
        ordered = sorted(people.values(), key=lambda p: (not p["overdue"], p["next_due"] or "9999", -p["balance"]))
        for p in ordered:
            p["balance"] = _money(p["balance"])
        return {"total": sum(p["balance"] for p in ordered),
                "overdue": sum(e["balance"] for p in ordered for e in p["entries"] if e["days_overdue"]),
                "people": ordered}

    return {"today": today.isoformat(), "owed_to_you": side(RECEIVABLE), "you_owe": side(PAYABLE)}


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


class AliasRemoval(BaseModel):
    alias: str


def _trader_source() -> Source:
    return Source(agent="trader", at=datetime.now(timezone.utc).isoformat(), turn_id=None)


@router.post("/people/{name}/aliases/remove")
async def remove_alias(name: str, body: AliasRemoval, db: AsyncSession = Depends(get_db_session),
                       user: User = Depends(get_current_user)):
    """"Not the same person": stop treating this name as them (kept as history)."""
    memory = get_graph_memory()
    try:
        found = await resolve_party(memory, user.id, name)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Nothing remembered about {name}.")
        closed = await memory.close_alias(user.id, " ".join(body.alias.lower().split()), found[0], _trader_source())
    except GraphMemoryError:
        raise HTTPException(status_code=503, detail="Memory is unavailable right now. Try again shortly.")
    if not closed:
        raise HTTPException(status_code=404, detail=f'"{body.alias}" isn\'t linked to {found[1]}.')
    await db.execute(delete(PartyAlias).where(PartyAlias.user_id == user.id,
                                              PartyAlias.alias_key == body.alias.strip().lower()))
    await db.commit()
    logger.info("dashboard.alias_removed", user_id=user.id)
    return {"removed": body.alias, "person": found[1]}


@router.post("/people/{name}/forget")
async def forget_person(name: str, db: AsyncSession = Depends(get_db_session),
                        user: User = Depends(get_current_user)):
    """Erase everything remembered about someone. Their entries in the books stay."""
    memory = get_graph_memory()
    try:
        found = await resolve_party(memory, user.id, name)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Nothing remembered about {name}.")
        counts = await Curator(memory, _session(db)).forget(user.id, found[0])
    except GraphMemoryError:
        raise HTTPException(status_code=503, detail="Memory is unavailable right now. Try again shortly.")
    return {"forgotten": found[1], "removed": counts}


@router.get("/history")
async def history(db: AsyncSession = Depends(get_db_session), user: User = Depends(get_current_user)):
    """The latest conversation turns, oldest first, so the chat survives a reload."""
    turns = (await db.execute(
        select(ConversationTurn).where(ConversationTurn.user_id == user.id)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(HISTORY_TURNS)
    )).scalars().all()
    return {"turns": [{"role": t.role, "content": t.content, "channel": t.channel,
                       "at": t.created_at.isoformat()} for t in reversed(turns)]}

"""The Memory curator: after each reply, in the background, decide what to remember.

Two kinds of work, deliberately separated:

1. Worked out by code, from the ledger (no model):
   - promises: an entry this turn got a due date → a Promise; an earlier open promise on
     the same debt becomes `replaced` (not yet due) or `broken` (already overdue)
   - inferred usual orders: the same items and quantities three sales in a row

   - notes the other agents' tools left for this turn: a bank name the trader confirmed
     (Alert matcher) and a reminder that was sent (Collector). Written here, after the
     message is saved, so they carry LEARNED_FROM the message and WRITTEN_BY that agent.

2. Proposed by the model, checked by code, written by fixed Cypher:
   aliases, stated usual orders, do-not-remind, and roles. The model returns operations
   (`MemoryOp`), never Cypher. Each one is validated: the person must exist, an alias must
   have been said, an alias can't swallow a separate person with their own records.
   Anything that fails is logged and dropped.

The curator never blocks or breaks a reply: it runs after the reply is saved, catches
everything, and does nothing at all when graph memory is off.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from config import LooseDate, business_today
from db import Party, PartyAlias, TransactionORM
from memory.models import GraphMemoryError, KnownName, Source
from memory.ports import GraphMemory
from memory.recall import Recalled, recall
from memory.sync import SessionFactory
from tools.ledger_events import ledger_events
from utils import logger, provider_problem

AGENT = "curator"
# forget_person deletes memory for good: only when the trader's own words ask for it
_FORGET_WORDS = ("forget", "delete", "erase", "remove", "clear", "comot", "wipe")
# do-not-remind takes someone off the chase list: only when the message is about reminding
# (the eval caught "give am till Friday" being read as "don't remind")
_REMIND_WORDS = ("remind", "chase", "disturb", "bother", "worry", "message", "text", "call", "leave am", "leave her",
                 "leave him", "no tell")
CURATOR_CALL_TIMEOUT = 60  # seconds; a stalled host must not hold the job worker
UNCHANGED = "unchanged"  # _apply's answer when the memory was already there
INFER_AFTER = 3  # identical sales in a row before a usual order is guessed


# ── what the model may propose ───────────────────────────────────────────────

class OrderItem(BaseModel):
    item: str
    quantity: float | None = None
    unit: str | None = None


class MemoryOp(BaseModel):
    op: Literal["add_alias", "remove_alias", "set_usual_order", "remove_usual_order", "set_do_not_remind",
                "lift_do_not_remind", "set_role", "forget_person"]
    party: str = Field(description="The person, by the name listed under People")
    alias: str | None = Field(default=None, description="add_alias: the other name, exactly as the trader wrote it")
    items: list[OrderItem] | None = Field(default=None, description="set_usual_order")
    payment: Literal["cash", "credit"] | None = Field(default=None, description="set_usual_order, if said")
    role: Literal["customer", "supplier"] | None = Field(default=None, description="set_role")
    active: bool | None = Field(default=None, description="set_role: false if the role ended")
    until: LooseDate = Field(default=None, description="set_do_not_remind: end date, if given")
    said: str | None = Field(default=None, description="The trader's own words, short")


class CuratorOutput(BaseModel):
    ops: list[MemoryOp] = Field(default_factory=list, description="Empty if there is nothing to remember")


INSTRUCTIONS = """You keep long-term memory for a Nigerian trader's bookkeeping assistant. From the trader's latest message (and the conversation just before it), list memory operations. Only what the trader clearly said; never guess. Most messages (sales, questions, reports, greetings) need none: return no ops.

Operations:
- add_alias: the trader confirmed two names are one person ("Chidi's mum na Mama Chidi", "yes" to "Is X the same as Y?", "Bolu na Bola"). Only the trader's own words count: never from the assistant's question alone, before the trader has answered. party = the known person; alias = the other name as written.
- set_usual_order: the trader says what someone usually buys ("Bola's usual na 2 bags rice"). Not for a single sale.
- set_do_not_remind: only when the trader says not to remind, chase or disturb someone ("no remind Pastor John"). until = end date if given. Giving someone more time to pay ("give am till Friday") is NOT this: that's a due date, already handled, so no op.
- lift_do_not_remind: only when the trader clearly says reminding them is now fine ("you fit remind Pastor John now"). Asking for a reminder ("remind Pastor John") is NOT permission (the assistant checks with them first) and isn't a new do-not-remind either: no op.
- set_role: a role started or ended. "Musa dey supply my oil now" → supplier, active true. "E no dey buy from me again" → customer, active false.
- remove_alias: the trader says a name is NOT that person ("Chidi's mum no be Mama Chidi"). party = the person, alias = the name.
- remove_usual_order: someone no longer has a usual order, or the remembered one is wrong.
- forget_person: only when the trader asks you to forget or delete what you know about someone. Never otherwise.
Use names exactly as listed under People. said = the trader's own short words.
Always answer with an object: {"ops": [...]}. Nothing to remember → {"ops": []}."""


# ── what one conversation turn hands the curator ────────────────────────────

@dataclass
class Turn:
    user_id: int
    turn_id: int | None           # conversation_turns id of the trader's message
    message: str
    reply: str
    channel: str = "web"
    history: list[dict] = field(default_factory=list)   # earlier turns, oldest first
    touched: list[dict] = field(default_factory=list)   # ledger entries written this turn (ledger.describe)
    recalled: Recalled = field(default_factory=Recalled)
    # left by tools during the turn: {"kind": "bank_alias", "alias", "party"} (Alert matcher)
    # or {"kind": "reminder_sent", "reminder_id", "party"} (Collector)
    notes: list[dict] = field(default_factory=list)
    today: date = field(default_factory=business_today)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_payload(self) -> dict:
        """For the job queue. Recall isn't kept: the worker recalls afresh when it runs."""
        return {"turn_id": self.turn_id, "message": self.message, "reply": self.reply, "channel": self.channel,
                "history": self.history, "touched": self.touched, "notes": self.notes,
                "today": self.today.isoformat(), "at": self.at}

    @classmethod
    def from_payload(cls, user_id: int, p: dict) -> "Turn":
        return cls(user_id=user_id, turn_id=p.get("turn_id"), message=p["message"], reply=p.get("reply", ""),
                   channel=p.get("channel", "web"), history=p.get("history") or [], touched=p.get("touched") or [],
                   notes=p.get("notes") or [], today=date.fromisoformat(p["today"]), at=p["at"])


@dataclass
class Outcome:
    promises: int = 0
    inferred: int = 0
    notes: list[str] = field(default_factory=list)   # notes written, by kind
    applied: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (op, reason)
    unchanged: list[str] = field(default_factory=list)              # proposed, but already known
    asked_model: bool = False
    graph_failed: bool = False  # the graph store failed before anything was written


def _structured(model: BaseChatModel):
    """The model constrained to CuratorOutput. JSON-schema mode where the provider has it:
    with tool calling, gpt-oss on Groq sometimes calls a made-up "commentary" tool, or no
    tool at all when there's nothing to remember, and the request fails."""
    try:
        return model.with_structured_output(CuratorOutput, method="json_schema")
    except (TypeError, ValueError, NotImplementedError):
        return model.with_structured_output(CuratorOutput)


def _empty_answer_rejected(e: Exception) -> bool:
    """Groq's JSON check refusing an empty answer: smaller models sometimes reply [] or {}
    for "nothing to remember", which isn't the schema's shape."""
    text = str(e)
    return "json_validate_failed" in text and any(f"'failed_generation': '{empty}'" in text
                                                   for empty in ("[]", "{}", ""))


def name_key(name: str) -> str:
    """Same normalisation as ledger party keys, with inner spaces collapsed."""
    return " ".join(name.strip().lower().split())


def _summary(items: list[OrderItem]) -> str:
    def one(i: OrderItem) -> str:
        qty = f"{i.quantity:g} " if i.quantity is not None else ""
        unit = f"{i.unit} " if i.unit else ""
        return f"{qty}{unit}{i.item}".strip()
    return ", ".join(one(i) for i in items)


class Curator:
    def __init__(self, memory: GraphMemory, session_factory: SessionFactory,
                 model_factory: Callable[[], BaseChatModel] | None = None,
                 tracer_factory: Callable[[int, str], object] | None = None,
                 retry_delay: float = 10.0):
        self._memory = memory
        self._retry_delay = retry_delay
        self._outbox = None  # (stage job, wake worker) when the durable queue is in use
        self._session_factory = session_factory
        self._model_factory = model_factory
        self._tracer_factory = tracer_factory
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._memory.enabled

    # ── delivery ────────────────────────────────────────────────────────────
    # The caller stages a turn before committing the reply, and calls committed() after.
    # In-process (tests, scripts): committed() schedules a background task. With the outbox
    # (the app): stage() writes a job in the reply's own transaction, so a saved reply
    # always gets curated, even across a crash; committed() wakes the worker.

    def use_outbox(self, stage_job, wake=None) -> None:
        self._outbox = (stage_job, wake) if stage_job else None

    def stage(self, db, turn: Turn) -> None:
        if self._outbox and self.enabled:
            self._outbox[0](db, "curate", turn.user_id, turn.to_payload())

    def committed(self, turn: Turn) -> None:
        if not self.enabled:
            return
        if self._outbox:
            self._outbox[1]()
        else:
            self.schedule(turn)

    async def handle_job(self, payload: dict, user_id: int) -> None:
        """A queued `curate` job. Raises RetryLater if the graph failed before anything was
        written (the queue retries); a failed model call isn't retried (it costs tokens)."""
        from jobs.queue import RetryLater
        turn = Turn.from_payload(user_id, payload)
        turn.recalled = await recall(self._memory, user_id, turn.message)
        outcome = await self.curate(turn)
        if outcome.graph_failed:
            raise RetryLater("graph store unavailable")

    def schedule(self, turn: Turn) -> None:
        """Run `curate` in the background. Never raises."""
        if not self.enabled:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._safe_curate(turn))
        except RuntimeError:
            logger.warning("curator.no_loop", user_id=turn.user_id)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _safe_curate(self, turn: Turn) -> None:
        try:
            outcome = await self.curate(turn)
            if outcome.graph_failed:
                # a blip in the graph store: try the turn once more. Safe to repeat: promises
                # and inferred orders are checked for before they're written
                await asyncio.sleep(self._retry_delay)
                logger.info("curator.retrying", user_id=turn.user_id, turn_id=turn.turn_id)
                await self.curate(turn)
        except Exception:
            logger.error("curator.crashed", user_id=turn.user_id, turn_id=turn.turn_id, exc_info=True)

    # ── the work ────────────────────────────────────────────────────────────

    async def curate(self, turn: Turn) -> Outcome:
        outcome = Outcome()
        if not self.enabled:
            return outcome
        started = time.perf_counter()
        # this turn's ledger changes must be in the graph before promises can attach to them
        await ledger_events.drain()
        source = Source(agent=AGENT, at=turn.at, turn_id=turn.turn_id)

        try:
            outcome.promises = await self._promises_from_due_dates(turn, source)
            outcome.inferred = await self._infer_usual_orders(turn)
            outcome.notes = await self._apply_notes(turn)
        except GraphMemoryError as e:
            logger.warning("curator.graph_failed", user_id=turn.user_id, step="ledger_derived", error=str(e))
            outcome.graph_failed = True
            return outcome

        if self._worth_asking(turn) and self._model_factory is not None:
            outcome.asked_model = True
            for op in await self._propose(turn):
                try:
                    reason = await self._apply(turn, op, source)
                except GraphMemoryError as e:
                    reason = f"graph error: {e}"
                if reason == UNCHANGED:
                    outcome.unchanged.append(op.op)
                elif reason:
                    outcome.rejected.append((op.op, reason))
                else:
                    outcome.applied.append(op.op)

        logger.info("curator.done", user_id=turn.user_id, turn_id=turn.turn_id, promises=outcome.promises,
                    inferred=outcome.inferred, notes=outcome.notes, asked_model=outcome.asked_model, applied=outcome.applied,
                    unchanged=outcome.unchanged, rejected=outcome.rejected, ms=round((time.perf_counter() - started) * 1000, 1))
        return outcome

    # 1a. promises, from the ledger
    async def _promises_from_due_dates(self, turn: Turn, source: Source) -> int:
        created = 0
        for entry in turn.touched:
            due = entry.get("due_date")
            if entry.get("type") not in ("sale", "loan_given") or not due or not entry.get("outstanding"):
                continue
            due = date.fromisoformat(due)
            pg_id = entry["id"]
            if await self._memory.debt_party(turn.user_id, pg_id) is None:
                logger.warning("curator.debt_missing", user_id=turn.user_id, pg_id=pg_id)
                continue
            existing = await self._memory.open_promises(turn.user_id, pg_id)
            if any(d == due.isoformat() for _, d in existing):
                continue  # the same promise, restated
            for promise_id, old_due in existing:
                if date.fromisoformat(old_due) < turn.today:
                    fraction = await self._paid_fraction(pg_id)
                    await self._memory.set_promise_status(turn.user_id, promise_id, "broken", fraction, turn.today)
                else:
                    await self._memory.set_promise_status(turn.user_id, promise_id, "replaced", None, turn.today)
            await self._memory.add_promise(turn.user_id, pg_id, due, source)
            created += 1
        return created

    async def forget(self, user_id: int, party_key: str) -> dict:
        """Erase what's remembered about a party, in the graph and in the bank-name table.
        The books (ledger entries) are untouched."""
        counts = await self._memory.forget_party(user_id, party_key)
        async with self._session_factory() as db:
            result = await db.execute(delete(PartyAlias).where(
                PartyAlias.user_id == user_id,
                PartyAlias.party_id.in_(select(Party.id).where(Party.user_id == user_id, Party.key == party_key))))
            await db.commit()
        counts["bank_names"] = result.rowcount or 0
        logger.info("curator.forgot", user_id=user_id, **counts)
        return counts

    async def _forget_bank_name(self, user_id: int, alias: str, party_key: str) -> int:
        """A name the trader says isn't this person: if it was a confirmed bank name, drop it
        from party_aliases (the store alert matching reads). Returns rows removed."""
        async with self._session_factory() as db:
            result = await db.execute(delete(PartyAlias).where(
                PartyAlias.user_id == user_id, PartyAlias.alias_key == alias.strip().lower(),  # as tools/alerts stores it
                PartyAlias.party_id.in_(select(Party.id).where(Party.user_id == user_id, Party.key == party_key))))
            await db.commit()
        return result.rowcount or 0

    async def _ledger_entries(self, user_id: int, key: str) -> int:
        async with self._session_factory() as db:
            return (await db.execute(
                select(func.count()).select_from(TransactionORM)
                .where(TransactionORM.user_id == user_id, TransactionORM.is_deleted.is_(False),
                       func.lower(func.trim(TransactionORM.party)) == key)
            )).scalar_one()

    async def _apply_notes(self, turn: Turn) -> list[str]:
        """What the other agents' tools asked to remember, with this turn as the source."""
        written = []
        for note in turn.notes:
            target = await self._resolve(turn.user_id, note.get("party") or "")
            if target is None:
                logger.warning("curator.note_unknown_party", user_id=turn.user_id, kind=note.get("kind"))
                continue
            if note.get("kind") == "bank_alias":
                source = Source(agent="alert_matcher", at=turn.at, turn_id=turn.turn_id)
                alias_key = name_key(note.get("alias") or "")
                if not alias_key or alias_key == target.party_key:
                    continue
                existing = await self._resolve(turn.user_id, note["alias"])
                if existing is not None:
                    if existing.party_key != target.party_key:
                        logger.warning("curator.bank_name_taken", user_id=turn.user_id)
                    continue
                await self._memory.add_alias(turn.user_id, alias_key, note["alias"].strip(), target.party_key,
                                             "bank_name", source)
                written.append("bank_alias")
            elif note.get("kind") == "reminder_sent":
                source = Source(agent="collector", at=turn.at, turn_id=turn.turn_id)
                await self._memory.record_reminder(turn.user_id, note["reminder_id"], target.party_key, source)
                written.append("reminder_sent")
        return written

    async def _paid_fraction(self, pg_id: int) -> float | None:
        async with self._session_factory() as db:
            row = await db.get(TransactionORM, pg_id)
            if row is None or not row.amount:
                return None
            return round(float((row.amount_paid or 0) / row.amount), 2)

    # 1b. usual orders, from the ledger
    async def _infer_usual_orders(self, turn: Turn) -> int:
        parties = {name_key(e["party"]) for e in turn.touched if e.get("type") == "sale" and e.get("party")}
        inferred = 0
        for key in parties:
            async with self._session_factory() as db:
                sales = (await db.execute(
                    select(TransactionORM)
                    .where(TransactionORM.user_id == turn.user_id, TransactionORM.type == "sale",
                           TransactionORM.is_deleted.is_(False), TransactionORM.status == "complete",
                           func.lower(func.trim(TransactionORM.party)) == key)
                    .order_by(TransactionORM.date.desc(), TransactionORM.id.desc())
                    .limit(INFER_AFTER)
                )).scalars().all()
            if len(sales) < INFER_AFTER:
                continue
            shapes = {((s.item or "").strip().lower(), s.quantity, (s.unit or "").strip().lower()) for s in sales}
            if len(shapes) != 1 or not sales[0].item:
                continue
            current = await self._memory.current_fact(turn.user_id, key, "usual_order")
            item = OrderItem(item=sales[0].item.strip().lower(), quantity=sales[0].quantity,
                             unit=(sales[0].unit or "").strip().lower() or None)
            summary = _summary([item])
            if current and (current["confidence"] == "stated" or current["summary"] == summary):
                continue  # what the trader said wins; and nothing new to learn
            payment = "cash" if all(s.amount_paid == s.amount for s in sales) else "credit"
            data = json.dumps({"items": [item.model_dump()], "payment": payment,
                               "last_amount": float(sales[0].amount), "source_entries": [s.id for s in sales]})
            await self._memory.set_fact(turn.user_id, key, "usual_order", summary,
                                        Source(agent=AGENT, at=turn.at, turn_id=None), "inferred", data=data)
            inferred += 1
        return inferred

    # 2. the model's proposals
    @staticmethod
    def _worth_asking(turn: Turn) -> bool:
        """Skip the model call when nothing in the turn could hold a memory: no known or
        possible person mentioned, no named entry recorded, no question just answered."""
        if not turn.message.strip():
            return False
        if not turn.recalled.is_empty() or any(e.get("party") for e in turn.touched):
            return True
        last_reply = next((h["content"] for h in reversed(turn.history) if h["role"] == "assistant"), "")
        return last_reply.rstrip().endswith("?")

    async def _propose(self, turn: Turn) -> list[MemoryOp]:
        people = sorted({p.name for p in turn.recalled.people}
                        | {n for names in turn.recalled.possible.values() for n in names}
                        | {e["party"] for e in turn.touched if e.get("party")})
        alias_lines = [f"{p.name} (also {', '.join(p.aliases)})" for p in turn.recalled.people if p.aliases]
        entries = [f"#{e['id']} {e['type']} {e.get('item') or ''} party={e.get('party')} due={e.get('due_date')}"
                   for e in turn.touched]
        convo = "\n".join(f"{h['role']}: {h['content']}" for h in turn.history[-4:])
        context = (f"Today: {turn.today.isoformat()}\n"
                   f"People: {', '.join(people) or 'none yet'}\n"
                   + (f"Known other names: {'; '.join(alias_lines)}\n" if alias_lines else "")
                   + (f"Entries recorded this turn: {'; '.join(entries)}\n" if entries else "")
                   + (f"Conversation before:\n{convo}\n" if convo else "")
                   + f"Trader's latest message: {turn.message}\nAssistant's reply: {turn.reply}")
        tracer = self._tracer_factory(turn.user_id, turn.channel) if self._tracer_factory else None
        config = {"run_name": "memory curator", **({"callbacks": [tracer]} if tracer else {})}
        try:
            messages = [SystemMessage(INSTRUCTIONS), HumanMessage(context)]
            base = self._model_factory()
            try:
                try:
                    result = await asyncio.wait_for(_structured(base).ainvoke(messages, config=config),
                                                    CURATOR_CALL_TIMEOUT)
                except (asyncio.TimeoutError, TimeoutError):
                    # a stalled host: one more try (background work, and every write is checked
                    # and repeat-safe, so a retry can't do harm)
                    logger.warning("curator.model_timeout_retry", user_id=turn.user_id)
                    result = await asyncio.wait_for(_structured(base).ainvoke(messages, config=config),
                                                    CURATOR_CALL_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                raise
            except Exception as e:
                if _empty_answer_rejected(e) or provider_problem(e):
                    raise
                # some hosts (e.g. behind OpenRouter) don't take JSON-schema output for every
                # model: one more try with plain tool calling before giving up on this turn
                logger.info("curator.json_schema_fallback", user_id=turn.user_id, error=type(e).__name__)
                result = await asyncio.wait_for(base.with_structured_output(CuratorOutput).ainvoke(messages, config=config),
                                                CURATOR_CALL_TIMEOUT)
        except Exception as e:
            if _empty_answer_rejected(e):
                # the model meant "nothing to remember" but wrote [] instead of {"ops": []}
                logger.debug("curator.nothing_to_remember", user_id=turn.user_id)
                return []
            logger.warning("curator.model_failed", user_id=turn.user_id, error=type(e).__name__,
                           problem=provider_problem(e), detail=str(e)[:300])
            return []
        if not isinstance(result, CuratorOutput):
            logger.warning("curator.model_bad_output", user_id=turn.user_id, got=type(result).__name__)
            return []
        return result.ops

    async def _resolve(self, user_id: int, name: str) -> KnownName | None:
        key = name_key(name)
        for known in await self._memory.known_names(user_id):
            if known.key == key:
                return known
        return None

    async def _apply(self, turn: Turn, op: MemoryOp, source: Source) -> str | None:
        """Validate and write one operation. Returns why it was rejected, or None."""
        target = await self._resolve(turn.user_id, op.party)
        if target is None:
            return "unknown person"
        party = target.party_key
        uid = turn.user_id

        if op.op == "add_alias":
            if not op.alias or not op.alias.strip():
                return "no alias given"
            alias_key = name_key(op.alias)
            if alias_key == party:
                return "alias is the same name"
            said_text = " ".join([turn.message] + [h["content"] for h in turn.history[-4:]]).lower()
            if alias_key not in " ".join(said_text.split()):
                return "alias not in the conversation"
            existing = await self._resolve(uid, op.alias)
            if existing is not None and existing.is_alias:
                return UNCHANGED if existing.party_key == party else "already another person's alias"
            if existing is not None:
                # the alias is itself a party (e.g. a misheard "Bolu"): only absorb it if it
                # has nothing of its own, in the ledger (a cash expense isn't a graph debt)
                # or the graph; otherwise they're two people
                if await self._ledger_entries(uid, alias_key) or \
                        not await self._memory.remove_empty_party(uid, alias_key):
                    return "a separate person with their own records"
            kind = "transcription" if "voice" in turn.message.lower() or turn.message.startswith("(voice note)") \
                else "nickname"
            await self._memory.add_alias(uid, alias_key, op.alias.strip(), party, kind, source)
            return None

        if op.op == "remove_alias":
            if not op.alias or not op.alias.strip():
                return "no alias given"
            closed = await self._memory.close_alias(uid, name_key(op.alias), party, source)
            dropped = await self._forget_bank_name(uid, op.alias, party)
            return None if closed or dropped else UNCHANGED

        if op.op == "remove_usual_order":
            closed = await self._memory.close_fact(uid, party, "usual_order", source)
            return None if closed else UNCHANGED

        if op.op == "forget_person":
            if not any(w in turn.message.lower() for w in _FORGET_WORDS):
                return "not asked to forget"
            await self.forget(uid, party)
            return None

        if op.op == "set_usual_order":
            items = [i for i in (op.items or []) if i.item and i.item.strip()]
            if not items:
                return "no items"
            current = await self._memory.current_fact(uid, party, "usual_order")
            if current and current["confidence"] == "stated" and current["summary"] == _summary(items):
                return UNCHANGED
            data = json.dumps({"items": [i.model_dump() for i in items], "payment": op.payment})
            await self._memory.set_fact(uid, party, "usual_order", _summary(items), source, "stated",
                                        said=op.said, data=data)
            return None

        if op.op == "set_do_not_remind":
            if not any(w in turn.message.lower() for w in _REMIND_WORDS):
                return "message isn't about reminding"
            if op.until and op.until < turn.today:
                return "end date already passed"
            current = await self._memory.current_fact(uid, party, "do_not_remind")
            if current and current["until"] == (op.until.isoformat() if op.until else None):
                return UNCHANGED  # the model often restates it from the history
            await self._memory.set_fact(uid, party, "do_not_remind", "do not remind", source, "stated",
                                        said=op.said, until=op.until)
            return None

        if op.op == "lift_do_not_remind":
            closed = await self._memory.close_fact(uid, party, "do_not_remind", source)
            return None if closed else UNCHANGED

        if op.op == "set_role":
            if op.role is None:
                return "no role given"
            await self._memory.set_stated_role(uid, party, op.role, op.active is not False, source)
            return None

        return f"unknown op {op.op}"

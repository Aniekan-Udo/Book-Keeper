"""Seed the demo: 30 days of Ngozi Provisions (docs/hackathon-scenario.md), twice.

    uv run python -m scripts.seed_demo            create the two demo accounts
    uv run python -m scripts.seed_demo --reset    delete them (and their graphs) first
    uv run python -m scripts.seed_demo --if-missing   only if they aren't there yet (deploys:
                                                      SEED_DEMO=true runs this on start)

- **Day 30** (ngozi.day30@example.com): the whole month replayed through the real code: the
  ledger, the graph sync, the memory curator (with the trader's messages as its source) and
  the Collector's daily sweeps, on the scenario's dates, moved forward by whole weeks so the
  month ends this week (weekdays stay the same).
- **Day 1** (ngozi.day1@example.com): exactly the same ledger, but nothing learned. Its graph
  only has what the ledger implies (people, debts, customers), like a trader on their first
  day of memory.

Send both the same messages ("Chidi's mum pay 30k", "who I suppose chase?", "why Emeka
first?") to see what graph memory changes. scripts/demo_check.py does that side by side.

No model is called: where the curator would ask the model, the scenario's expected answer
is given to it, so seeding is free, repeatable and doesn't depend on the model's mood. The
curator still validates and writes everything itself. All data is made up (Rule 6).

Password for both accounts: DEMO_PASSWORD from the environment, else a random one printed
at the end.
"""
import argparse
import asyncio
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select

from authentication.auth import hash_password
from config import business_today
from endpoints.demo import DAY1, DAY30
from db import (
    BankAlert, Contact, ConversationTurn, IdempotencyRecord, OnboardingSession, Party, PartyAlias, PhoneChallenge, Reminder,
    RepaymentAllocation, StatementLink, TransactionORM, User, UserSession, engine, get_db, init_models,
)
from memory import build_graph_memory
from memory.collector import Collector
from memory.curator import Curator, CuratorOutput, MemoryOp, OrderItem, Turn
from memory.recall import recall
from memory.sync import GraphSync
from tools.alerts import save_alias
from tools.ledger import TransactionInput, correct_transaction, persist_transactions
from tools.ledger_events import ledger_events



# The scenario is written for September 2026. Seeding shifts it forward by whole weeks so its
# last day falls in the current week: "this week" and "this month" then have real numbers,
# overdue counts are recent, and every weekday stays true ("he go pay Monday" is a Monday).
SCENARIO_END = date(2026, 9, 30)
SHIFT = timedelta(weeks=max(0, (business_today() - SCENARIO_END).days // 7))


def d(day: int) -> date:
    return date(2026, 9, day) + SHIFT


def sale(party, item, qty, unit, amount, day, paid=None, due=None):
    return TransactionInput(type="sale", party=party, item=item, quantity=qty, unit=unit, amount=amount,
                            amount_paid=paid, direction="in", date=d(day), due_date=d(due) if due else None)


def repaid(party, amount, day):
    return TransactionInput(type="debt_repayment", party=party, amount=amount, direction="in", date=d(day))


def bought(party, item, qty, unit, amount, day, paid=None):
    return TransactionInput(type="expense", party=party, item=item, quantity=qty, unit=unit, amount=amount,
                            amount_paid=paid, direction="out", date=d(day))


@dataclass
class Step:
    day: int
    message: str
    reply: str
    entries: list[TransactionInput] = field(default_factory=list)
    label: str | None = None                      # name the entries, for later corrections
    correct: tuple[str, dict] | None = None       # (label, changes)
    ops: list[MemoryOp] = field(default_factory=list)   # what the curator's model would propose
    notes: list[dict] = field(default_factory=list)      # what the agents' tools would leave
    sweep: bool = False                           # the Collector's daily run, before this step


def alert(amount: str, sender: str) -> str:
    return f"(forwarded alert) Credit Alert! Amt: NGN{amount} Desc: TRF FROM {sender} Avail Bal: ****"


MONTH = [
    Step(2, "Emeka collect 2 bags beans, 60k. He go pay Monday",
         "Recorded: Emeka, 2 bags beans, ₦60,000 on credit, due Monday Sep 7.",
         [sale("Emeka", "beans", 2, "bags", 60000, 2, paid=0, due=7)], label="E1"),
    Step(3, "Mama Chidi take 1 bag rice on credit, she go pay Friday",
         "Recorded: Mama Chidi, 1 bag rice, ₦45,000 on credit, due Friday Sep 4.",
         [sale("Mama Chidi", "rice", 1, "bag", 45000, 3, paid=0, due=4)]),
    Step(4, alert("45,000.00", "OKAFOR CHINYERE MARY"),
         "₦45,000 in from OKAFOR CHINYERE MARY. Recorded as Mama Chidi's payment; her debt is settled.",
         [repaid("Mama Chidi", 45000, 4)],
         notes=[{"kind": "bank_alias", "alias": "OKAFOR CHINYERE MARY", "party": "Mama Chidi"}]),
    Step(5, "Bola buy 2 bags rice, 90k cash", "Recorded: Bola, 2 bags rice, ₦90,000 cash.",
         [sale("Bola", "rice", 2, "bags", 90000, 5)]),
    Step(6, "Pastor John take 1 bag beans for church program, 30k, e never pay",
         "Recorded: Pastor John, 1 bag beans, ₦30,000, not paid yet.",
         [sale("Pastor John", "beans", 1, "bag", 30000, 6, paid=0)]),
    Step(7, "Alhaji Musa buy 5 bags garri, 75k, credit", "Recorded: Alhaji Musa, 5 bags garri, ₦75,000 on credit.",
         [sale("Alhaji Musa", "garri", 5, "bags", 75000, 7, paid=0)]),
    Step(8, "No remind Pastor John o, I go talk to am myself", "Okay, I won't remind Pastor John.", sweep=True,
         ops=[MemoryOp(op="set_do_not_remind", party="Pastor John",
                       said="No remind Pastor John o, I go talk to am myself")]),
    Step(8, "Iya Toyin collect 4 bags garri, 60k. She say next week Tuesday",
         "Recorded: Iya Toyin, 4 bags garri, ₦60,000 on credit, due Tuesday Sep 15.",
         [sale("Iya Toyin", "garri", 4, "bags", 60000, 8, paid=0, due=15)]),
    Step(9, "Emeka beg make I give am till Friday", "Updated: Emeka's ₦60,000 is now due Friday Sep 11.",
         correct=("E1", {"due_date": d(11)})),
    Step(9, "Alhaji Musa don pay the 75k cash", "Recorded: Alhaji Musa paid ₦75,000. Settled.",
         [repaid("Alhaji Musa", 75000, 9)]),
    Step(10, "Chidi's mum take 2 bags garri, 30k, Friday", "Is Chidi's mum Mama Chidi, or someone new?"),
    Step(10, "na Mama Chidi", "Recorded: Mama Chidi, 2 bags garri, ₦30,000 on credit, due Friday Sep 11.",
         [sale("Mama Chidi", "garri", 2, "bags", 30000, 10, paid=0, due=11)],
         ops=[MemoryOp(op="add_alias", party="Mama Chidi", alias="Chidi's mum")]),
    Step(11, alert("30,000.00", "OKAFOR CHINYERE MARY"),
         "₦30,000 in from Mama Chidi (OKAFOR CHINYERE MARY). Recorded; her debt is settled.",
         [repaid("Mama Chidi", 30000, 11)]),
    Step(12, "Bola buy 2 bags rice again, cash", "Recorded: Bola, 2 bags rice, ₦90,000 cash.",
         [sale("Bola", "rice", 2, "bags", 90000, 12)], sweep=True),
    Step(12, "I pay Chidi 2k to carry the bags", "Recorded: ₦2,000 to Chidi for carrying.",
         [bought("Chidi", "carrying bags", None, None, 2000, 12)]),
    Step(14, alert("60,000.00", "NWOSU EMEKA CHUKWUDI"), "Whose payment is this ₦60,000 from NWOSU EMEKA CHUKWUDI?"),
    Step(14, "na Emeka", "Recorded: Emeka paid ₦60,000. His debt is settled.", [repaid("Emeka", 60000, 14)],
         notes=[{"kind": "bank_alias", "alias": "NWOSU EMEKA CHUKWUDI", "party": "Emeka"}]),
    Step(15, alert("40,000.00", "ADEBAYO TOYIN F"), "Whose payment is this ₦40,000 from ADEBAYO TOYIN F?"),
    Step(15, "na Iya Toyin", "Recorded: Iya Toyin paid ₦40,000. ₦20,000 still owed.",
         [repaid("Iya Toyin", 40000, 15)],
         notes=[{"kind": "bank_alias", "alias": "ADEBAYO TOYIN F", "party": "Iya Toyin"}]),
    Step(15, "Emeka take 1 bag rice, 45k, Friday", "Recorded: Emeka, 1 bag rice, ₦45,000 on credit, due Friday Sep 18.",
         [sale("Emeka", "rice", 1, "bag", 45000, 15, paid=0, due=18)]),
    Step(16, "Alhaji Musa no dey buy from me again, na him dey supply my palm oil now. I buy 10 tins, 120k, pay am 70k",
         "Recorded: bought 10 tins palm oil from Alhaji Musa for ₦120,000, paid ₦70,000. You owe him ₦50,000.",
         [bought("Alhaji Musa", "palm oil", 10, "tins", 120000, 16, paid=70000)], sweep=True,
         ops=[MemoryOp(op="set_role", party="Alhaji Musa", role="customer", active=False),
              MemoryOp(op="set_role", party="Alhaji Musa", role="supplier", active=True)]),
    Step(18, "Emeka pay the 45k cash", "Recorded: Emeka paid ₦45,000. Settled.", [repaid("Emeka", 45000, 18)]),
    Step(19, "(voice note) Bolu buy 2 bags rice, cash", "Recorded: Bolu, 2 bags rice, ₦90,000 cash.",
         [sale("Bolu", "rice", 2, "bags", 90000, 19)], label="BOLU"),
    Step(19, "Bolu na Bola, the voice thing spell am wrong", "Fixed: that sale is now under Bola.",
         correct=("BOLU", {"party": "Bola"}), ops=[MemoryOp(op="add_alias", party="Bola", alias="Bolu")]),
    Step(21, "Emeka take 3 tins oil, 36k. He go pay Thursday",
         "Recorded: Emeka, 3 tins palm oil, ₦36,000 on credit, due Thursday Sep 24.",
         [sale("Emeka", "palm oil", 3, "tins", 36000, 21, paid=0, due=24)]),
    Step(22, "Iya Toyin bring the remaining 20k cash", "Recorded: Iya Toyin paid ₦20,000. Settled.",
         [repaid("Iya Toyin", 20000, 22)]),
    Step(24, "Mama C take 1 bag rice", "Is Mama C Mama Chidi?"),
    Step(24, "yes", "Recorded: Mama Chidi, 1 bag rice, ₦45,000 on credit.",
         [sale("Mama Chidi", "rice", 1, "bag", 45000, 24, paid=0)],
         ops=[MemoryOp(op="add_alias", party="Mama Chidi", alias="Mama C")]),
    Step(25, alert("45,000.00", "OKAFOR CHINYERE MARY"),
         "₦45,000 in from Mama Chidi (OKAFOR CHINYERE MARY). Recorded; her debt is settled.",
         [repaid("Mama Chidi", 45000, 25)], sweep=True),
    Step(26, "Wetin you know about Bola?", "Bola usually buys 2 bags of rice and pays cash. Also called Bolu."),
    Step(29, "Mama Chidi take 1 bag beans", "Recorded: Mama Chidi, 1 bag beans, ₦30,000 on credit.",
         [sale("Mama Chidi", "beans", 1, "bag", 30000, 29, paid=0)]),
]


class Script:
    """Stands in for the curator's model: answers with the step's expected operations."""

    def __init__(self):
        self.ops: list[MemoryOp] = []

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages, config=None):
        return CuratorOutput(ops=self.ops)


async def make_user(email: str, business: str, password: str) -> int:
    async with get_db() as db:
        user = User(firstname="Ngozi", lastname="Eze", email=email, password=hash_password(password),
                    business_name=business)
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
    return uid


async def delete_demo_user(email: str, memory) -> None:
    async with get_db() as db:
        uid = (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
        if uid is None:
            return
        txn_ids = select(TransactionORM.id).where(TransactionORM.user_id == uid)
        await db.execute(delete(RepaymentAllocation).where(RepaymentAllocation.repayment_id.in_(txn_ids)))
        for model in (BankAlert, PartyAlias, Reminder, Contact, StatementLink, ConversationTurn, UserSession,
                      IdempotencyRecord, TransactionORM, Party):
            await db.execute(delete(model).where(model.user_id == uid))
        await db.execute(delete(PhoneChallenge).where(PhoneChallenge.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    await memory.delete_trader_graph(uid)
    print(f"deleted {email} (user {uid}) and its graph")


async def replay(uid: int, memory, learn: bool) -> None:
    """The month, in order. `learn=False` skips everything but the ledger (the day-1 account)."""
    script = Script()
    curator = Curator(memory, get_db, model_factory=lambda: script, retry_delay=0)
    collector = Collector(memory, get_db)
    labels: dict[str, int] = {}
    history: list[dict] = []
    for step in MONTH:
        at = datetime.combine(d(step.day), time(12), tzinfo=timezone.utc)
        if learn and step.sweep:
            await collector.sweep(uid, d(step.day))
        touched = []
        async with get_db() as db:
            if step.entries:
                result = await persist_transactions(db, step.entries, uid)
                touched = result["logged"]
                if step.label:
                    labels[step.label] = touched[0]["id"]
            if step.correct:
                label, changes = step.correct
                touched = [(await correct_transaction(db, uid, labels[label], **changes))["updated"]]
            if learn:
                # what resolving the alert did: the bank name goes in party_aliases (the store
                # alert matching reads); the curator mirrors it into the graph below
                for note in step.notes or []:
                    if note.get("kind") == "bank_alias":
                        await save_alias(db, uid, note["alias"], note["party"])
                await db.commit()
        await ledger_events.drain()
        if not learn:
            continue
        async with get_db() as db:
            user_turn = ConversationTurn(user_id=uid, role="user", content=step.message, channel="whatsapp",
                                         created_at=at)
            db.add_all([user_turn, ConversationTurn(user_id=uid, role="assistant", content=step.reply,
                                                    channel="whatsapp", created_at=at)])
            await db.flush()
            turn_id = user_turn.id
            await db.commit()
        script.ops = step.ops
        await curator.curate(Turn(user_id=uid, turn_id=turn_id, message=step.message, reply=step.reply,
                                  channel="whatsapp", history=history[-4:], touched=touched,
                                  recalled=await recall(memory, uid, step.message), notes=step.notes,
                                  today=d(step.day), at=at.isoformat()))
        history += [{"role": "user", "content": step.message}, {"role": "assistant", "content": step.reply}]
    if learn:
        await collector.sweep(uid, d(30))


async def main(reset: bool, if_missing: bool = False) -> int:
    memory = build_graph_memory()
    if not memory.enabled or not await memory.ping():
        print("Graph memory is off or FalkorDB isn't reachable (docker compose up -d falkordb).")
        return 1
    await init_models()
    sync = GraphSync(memory, get_db, retry_delay=0)
    ledger_events.subscribe(sync.on_ledger_changed)
    password = os.getenv("DEMO_PASSWORD") or secrets.token_urlsafe(9)
    try:
        if if_missing:
            found = 0
            for email, _ in (DAY30, DAY1):
                async with get_db() as db:
                    found += (await db.execute(select(User.id).where(User.email == email))).first() is not None
            if found == 2:
                print("Demo accounts already seeded.")
                return 0
            reset = True  # none, or half of a seed that was cut off: start them over
        for email, _ in (DAY30, DAY1):
            async with get_db() as db:
                exists = (await db.execute(select(User.id).where(User.email == email))).first()
            if exists and not reset:
                print(f"{email} already exists. Run with --reset to recreate the demo accounts.")
                return 1
            if exists:
                await delete_demo_user(email, memory)
        for (email, business), learn in ((DAY30, True), (DAY1, False)):
            uid = await make_user(email, business, password)
            await replay(uid, memory, learn)
            print(f"{email}: user {uid}, graph trader_{uid} ({'full month of memory' if learn else 'ledger only'})")
    finally:
        ledger_events.unsubscribe(sync.on_ledger_changed)
        await memory.close()
        await engine.dispose()
    print(f"\nPassword for both: {password}")
    print("Explore the graphs in FalkorDB Browser: http://localhost:3000")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the demo accounts and their graphs first")
    parser.add_argument("--if-missing", action="store_true", help="do nothing if both demo accounts exist")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.reset, args.if_missing)))

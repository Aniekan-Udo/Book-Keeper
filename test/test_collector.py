"""Day 3: the Collector (broken promises, who to chase and why, do-not-remind enforced),
reminders and bank names remembered with their source, the Alert matcher using them, and
the "why?" tool. Real FalkorDB (skipped if none), real ledger, no model."""
from datetime import date

import pytest

from db import BankAlert, ConversationTurn, Contact, ReminderOptOut
from memory import set_graph_memory
from memory.collector import Collector
from memory.curator import Turn
from memory.explain import explain
from memory.models import Source
from memory.null import NullGraphMemory
from tools import alerts
from tools.clean_version import build_tools
from tools.ledger import TransactionInput
from tools.ledger_events import ledger_events

from test.test_memory_curator import credit_sale, session_factory, world  # noqa: F401

TODAY = date(2026, 9, 30)
SRC = Source(agent="curator", at="2026-09-01T10:00:00Z", turn_id=None)


async def promise(w, entry, due, status=None, fraction=None):
    pid = await w.mem.add_promise(w.user_id, entry["id"], due, SRC)
    if status:
        await w.mem.set_promise_status(w.user_id, pid, status, fraction, due)
    return pid


@pytest.fixture
def graph_on(world):
    set_graph_memory(world.mem)
    yield
    set_graph_memory(NullGraphMemory())


# ── sweep: deciding broken / kept ────────────────────────────────────────────

async def test_sweep_breaks_overdue_promises_and_records_how_much_was_paid(world):
    [entry] = await world.record(credit_sale("Iya Toyin", 60000, due=date(2026, 9, 15), paid=0))
    await promise(world, entry, date(2026, 9, 15))
    await world.record(TransactionInput(type="debt_repayment", party="Iya Toyin", amount=40000, direction="in",
                                        date=date(2026, 9, 15)))

    counts = await Collector(world.mem, session_factory(world.db)).sweep(world.user_id, TODAY)

    assert counts == {"broken": 1, "kept": 0}
    assert await world.rows("MATCH (pr:Promise) RETURN pr.status, pr.paid_fraction, pr.closed_by") \
        == [["broken", 0.67, "collector"]]


async def test_sweep_counts_a_late_payment_as_broken(world):
    [entry] = await world.record(credit_sale("Emeka", 36000, due=date(2026, 9, 24), day=date(2026, 9, 21)))
    await promise(world, entry, date(2026, 9, 24))
    await world.record(TransactionInput(type="debt_repayment", party="Emeka", amount=36000, direction="in",
                                        date=date(2026, 9, 28)))

    await Collector(world.mem, session_factory(world.db)).sweep(world.user_id, TODAY)
    assert await world.rows("MATCH (pr:Promise) RETURN pr.status, pr.paid_fraction") == [["broken", 1.0]]


async def test_sweep_leaves_promises_not_yet_due(world):
    [entry] = await world.record(credit_sale("Emeka", 36000, due=date(2026, 10, 2)))
    await promise(world, entry, date(2026, 10, 2))
    assert await Collector(world.mem, session_factory(world.db)).sweep(world.user_id, TODAY) == {"broken": 0, "kept": 0}


# ── who to chase ─────────────────────────────────────────────────────────────

async def scenario_day_30(w):
    """Balances and promise history going into day 30 of docs/hackathon-scenario.md."""
    e1, = await w.record(credit_sale("Emeka", 60000, due=date(2026, 9, 7)))
    for due, status in ((date(2026, 9, 7), "broken"), (date(2026, 9, 11), "broken")):
        await promise(w, e1, due, status, 0.0)
    await w.record(TransactionInput(type="debt_repayment", party="Emeka", amount=60000, direction="in",
                                    date=date(2026, 9, 14)))
    e3, = await w.record(credit_sale("Emeka", 45000, due=date(2026, 9, 18), day=date(2026, 9, 15)))
    await promise(w, e3, date(2026, 9, 18), "kept", 1.0)
    await w.record(TransactionInput(type="debt_repayment", party="Emeka", amount=45000, direction="in",
                                    date=date(2026, 9, 18)))
    e4, = await w.record(credit_sale("Emeka", 36000, due=date(2026, 9, 24), day=date(2026, 9, 21)))
    await promise(w, e4, date(2026, 9, 24))                     # the sweep will break this one

    m, = await w.record(credit_sale("Mama Chidi", 30000, day=date(2026, 9, 29)))
    for due in (date(2026, 9, 4), date(2026, 9, 11)):
        await promise(w, m, due, "kept", 1.0)

    await w.record(credit_sale("Pastor John", 30000, day=date(2026, 9, 6)))
    await w.mem.set_fact(w.user_id, "pastor john", "do_not_remind", "do not remind",
                         Source(agent="curator", at="2026-09-08T09:00:00Z", turn_id=None), "stated",
                         said="No remind Pastor John o, I go talk to am myself")


async def test_who_to_chase_on_day_30(world):
    await scenario_day_30(world)
    result = await Collector(world.mem, session_factory(world.db)).who_to_chase(world.user_id, TODAY)

    assert [d["name"] for d in result["chase"]] == ["Emeka", "Mama Chidi"]
    emeka = result["chase"][0]
    assert emeka["owes"] == "₦36,000.00"
    assert "promised to pay by Sep 24 and hasn't (6 days ago)" in emeka["reasons"]
    assert "has broken 3 of 4 promises" in emeka["reasons"]
    assert "kept all 2 promises so far" in result["chase"][1]["reasons"]
    [skipped] = result["skipped"]
    assert skipped["name"] == "Pastor John"
    assert skipped["reason"] == 'you said on Sep 8 not to remind them ("No remind Pastor John o, I go talk to am myself")'
    assert result["ranked_by"].startswith("broken promises")


async def test_without_memory_the_ranking_is_amount_and_overdue_only(world):
    """The "day 1" contrast: same ledger, nothing remembered."""
    await scenario_day_30(world)
    result = await Collector(NullGraphMemory(), session_factory(world.db)).who_to_chase(world.user_id, TODAY)

    assert [d["name"] for d in result["chase"]] == ["Emeka", "Pastor John", "Mama Chidi"]  # Pastor John not skipped
    assert result["skipped"] == [] and result["ranked_by"].startswith("days overdue")


async def test_someone_who_replied_stop_is_skipped(world):
    await world.record(credit_sale("Emeka", 36000))
    world.db.add_all([Contact(user_id=world.user_id, name="Emeka", name_key="emeka", phone="+2348051112222"),
                      ReminderOptOut(phone="+2348051112222")])
    await world.db.commit()
    result = await Collector(world.mem, session_factory(world.db)).who_to_chase(world.user_id, TODAY)
    assert result["chase"] == [] and result["skipped"][0]["reason"] == "replied STOP to reminders"


# ── do-not-remind, enforced in the reminder tool ─────────────────────────────

async def test_remind_tool_refuses_until_the_trader_confirms(world, graph_on):
    await scenario_day_30(world)

    def remind(last_reply=""):
        return {t.name: t for t in build_tools(world.db, world.user_id, [], last_reply=last_reply)}["remind_debtor_tool"]

    refused = await remind().ainvoke({"party": "Pastor John"})
    assert "Not drafted: you said on Sep 8 not to remind them" in refused["needs_confirmation"]
    # the model can't just set the flag: the user must have been asked
    still = await remind().ainvoke({"party": "Pastor John", "trader_confirmed": True})
    assert "needs_confirmation" in still
    asked = "You said on Sep 8 not to remind Pastor John. You still want me to remind him?"
    allowed = await remind(asked).ainvoke({"party": "Pastor John", "trader_confirmed": True})
    assert "needs_confirmation" not in allowed  # on to the normal draft rules (reminders are off in tests)


# ── notes from the agents' tools, written with their source ─────────────────

async def test_bank_name_and_reminder_notes_are_remembered_with_their_source(world):
    await scenario_day_30(world)
    curator = world.curator()
    turn = Turn(user_id=world.user_id, turn_id=301, message="na Emeka", reply="ok", today=TODAY, notes=[
        {"kind": "bank_alias", "alias": "NWOSU EMEKA CHUKWUDI", "party": "Emeka"},
        {"kind": "reminder_sent", "reminder_id": 77, "party": "Emeka"},
    ])
    await Collector(world.mem, session_factory(world.db)).sweep(world.user_id, TODAY)
    outcome = await curator.curate(turn)

    assert outcome.notes == ["bank_alias", "reminder_sent"]
    assert await world.rows("MATCH (a:Alias)-[:WRITTEN_BY]->(ag:Agent), (a)-[:LEARNED_FROM]->(m:Message) "
                            "RETURN a.text, a.kind, ag.name, m.turn_id") \
        == [["NWOSU EMEKA CHUKWUDI", "bank_name", "alert_matcher", 301]]
    assert await world.rows("MATCH (r:Reminder)-[:SENT_TO]->(p:Party), (r)-[:WRITTEN_BY]->(ag:Agent) "
                            "RETURN r.pg_id, p.key, ag.name") == [[77, "emeka", "collector"]]
    assert sorted(await world.rows("MATCH (:Reminder)-[:BECAUSE]->(pr:Promise) RETURN pr.due")) \
        == [["2026-09-07"], ["2026-09-11"], ["2026-09-24"]]  # the broken ones; kept ones aren't a reason


async def test_alert_matcher_uses_a_remembered_bank_name(world):
    await world.record(credit_sale("Emeka", 36000), credit_sale("Nwosu", 20000))
    alert = BankAlert(user_id=world.user_id, direction="in", amount=36000, counterparty="NWOSU EMEKA CHUKWUDI",
                      fingerprint="x", kind="credit_alert", status="pending")
    # "Emeka" and "Nwosu" (another customer) both match the bank name by words: ambiguous
    assert len(await alerts.matching_debtors(world.db, world.user_id, alert)) == 2

    # a graph alias alone doesn't decide where money goes: party_aliases is the one store
    await world.mem.add_alias(world.user_id, "nwosu emeka chukwudi", "NWOSU EMEKA CHUKWUDI", "nwosu", "bank_name",
                              Source(agent="alert_matcher", at="2026-09-14T10:00:00Z", turn_id=None))
    assert len(await alerts.matching_debtors(world.db, world.user_id, alert)) == 2

    await alerts.save_alias(world.db, world.user_id, "NWOSU EMEKA CHUKWUDI", "Emeka")  # the trader confirmed it
    await world.db.commit()
    assert list(await alerts.matching_debtors(world.db, world.user_id, alert)) == ["emeka"]


# ── why? ─────────────────────────────────────────────────────────────────────

async def test_why_answers_from_the_graph_with_the_traders_words(world):
    world.db.add(ConversationTurn(id=101, user_id=world.user_id, role="user", channel="whatsapp",
                                  content="Emeka collect 2 bags beans, 60k. He go pay Monday"))
    await world.db.commit()
    [entry] = await world.record(credit_sale("Emeka", 60000, due=date(2026, 9, 7)))
    await world.curator().curate(await world.turn("Emeka collect 2 bags beans, 60k. He go pay Monday",
                                                  touched=[entry], today=date(2026, 9, 2), turn_id=101))
    await Collector(world.mem, session_factory(world.db)).sweep(world.user_id, TODAY)

    answer = await explain(world.mem, session_factory(world.db), world.user_id, "emeka")

    [p] = answer["promises"]
    assert p["due"] == "2026-09-07" and p["outcome"] == "broken" and p["paid"] == "0%"
    assert p["you_said"] == "Emeka collect 2 bags beans, 60k. He go pay Monday" and p["written_by"] == "curator"
    assert p["decided_by"] == "collector"
    assert answer["promise_record"] == "kept 0, broken 1"
    assert answer["roles"] == [{"role": "customer", "status": "current", "how": "from the records"}]


async def test_why_about_someone_unknown(world):
    assert "don't have anything remembered" in (await explain(world.mem, session_factory(world.db), world.user_id,
                                                              "Nobody"))["error"]


async def test_collector_tools_are_wired(world, graph_on):
    await scenario_day_30(world)
    tools = {t.name: t for t in build_tools(world.db, world.user_id, [])}
    ranked = await tools["who_to_chase_tool"].ainvoke({})
    assert ranked["chase"][0]["name"] == "Emeka"
    why = await tools["explain_tool"].ainvoke({"person": "Pastor John"})
    assert why["remembered"][0]["what"] == "do not remind"

"""Recall (finding people in a message) and the Memory curator (deciding what to remember).

Recall's matching is pure and always runs. The curator tests run against a real FalkorDB
(docker compose up -d falkordb) and are skipped if none answers; the model is scripted, so
these test the checking and writing, not the LLM's judgement (that's the eval's job).
"""
import os
from contextlib import asynccontextmanager
from datetime import date

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage

from memory.curator import Curator, CuratorOutput, MemoryOp, OrderItem, Turn
from memory.falkor import FalkorGraphMemory
from memory.models import GraphMemoryError, KnownName, PartyMemory, PromiseView, FactView
from memory.recall import Recalled, match_names, prompt_section, recall
from memory.sync import GraphSync
from tools.ledger import TransactionInput, correct_transaction, persist_transactions
from tools.ledger_events import ledger_events


# ── recall: matching names (pure) ────────────────────────────────────────────

def known(*names, aliases=()):
    out = [KnownName(key=n.lower(), party_key=n.lower(), party_name=n, is_alias=False) for n in names]
    out += [KnownName(key=a.lower(), party_key=p.lower(), party_name=p, is_alias=True) for a, p in aliases]
    return out


SCENARIO = known("Mama Chidi", "Chidi", "Emeka", "Pastor John", "Bola", "Bolu")


@pytest.mark.parametrize("message, exact, possible", [
    ("Mama Chidi pay 30k", {"mama chidi"}, {}),                        # not also Chidi
    ("I pay Chidi 2k to carry the bags", {"chidi"}, {}),
    ("Chidi's mum take 2 bags garri, 30k", set(), {"chidi": ["Chidi", "Mama Chidi"]}),  # ask which
    ("I pay Chidi 2k", {"chidi"}, {}),
    ("Mama C take 1 bag rice", set(), {"mama c": ["Mama Chidi"]}),
    ("No remind Pastor John o", {"pastor john"}, {}),
    ("Bolu na Bola, the voice thing spell am wrong", {"bolu", "bola"}, {}),
    ("Mama Nkechi buy rice", set(), {}),                               # "mama" alone is nobody
    ("I sold rice 5k", set(), {}),
])
def test_matching_names_in_a_message(message, exact, possible):
    got_exact, got_possible, _, _ = match_names(message, SCENARIO)
    assert got_exact == exact
    assert {k: sorted(v) for k, v in got_possible.items()} == possible


def test_a_learned_alias_is_an_exact_match():
    names = SCENARIO + known(aliases=[("Chidi's mum", "Mama Chidi")])
    exact, possible, aliases_used, _ = match_names("Chidi's mum pay 30k", names)
    assert exact == {"mama chidi"} and possible == {} and aliases_used == {"chidi's mum": "Mama Chidi"}


def test_prompt_section_says_what_is_remembered_and_what_to_ask():
    recalled = Recalled(
        people=[PartyMemory(key="emeka", name="Emeka", roles=("customer",), open_debts=1,
                            promises=(PromiseView("2026-09-07", "broken"), PromiseView("2026-09-18", "kept"),
                                      PromiseView("2026-09-24", "open"))),
                PartyMemory(key="bola", name="Bola", facts=(FactView("usual_order", "2 bags rice", None, None,
                                                                     "inferred",
                                                                     data='{"payment": "cash", "last_amount": 90000}'),))],
        possible={"chidi": ["Chidi", "Mama Chidi"]},
        aliases_used={"mama c": "Mama Chidi"},
    )
    text = prompt_section(recalled)
    assert "Emeka: customer; 1 open debt; promised to pay by 2026-09-24; promises kept 1, broken 1" in text
    assert "usual order: 2 bags rice, last time ₦90,000, paid cash (guessed from past sales; confirm)" in text
    assert '"mama c" means Mama Chidi: record entries under "Mama Chidi"' in text
    assert '"chidi" could be Chidi or Mama Chidi: ask which' not in text  # weak here: not in `strong`
    assert '"chidi" is not Chidi or Mama Chidi: a different person unless the user says so' in text
    strong = prompt_section(Recalled(possible={"chidi": ["Chidi", "Mama Chidi"]}, strong=frozenset({"chidi"})))
    assert '"chidi" could be Chidi or Mama Chidi: ask which' in strong
    assert prompt_section(Recalled()) == ""


async def test_recall_without_a_graph_returns_nothing():
    class Down:
        enabled = True

        async def known_names(self, user_id):
            raise GraphMemoryError("down")

    assert (await recall(Down(), 1, "Emeka paid")).is_empty()


# ── curator (real FalkorDB, scripted model) ──────────────────────────────────

FALKOR_HOST = os.getenv("FALKORDB_TEST_HOST", "127.0.0.1")
FALKOR_PORT = int(os.getenv("FALKORDB_TEST_PORT", "6379"))


class ScriptedModel:
    """Stands in for the chat model: returns the queued ops, records what it was asked."""

    def __init__(self, ops=(), fail=False):
        self.ops = list(ops)
        self.fail = fail
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        assert schema is CuratorOutput
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages[-1].content)
        if self.fail:
            raise RuntimeError("429 rate limited")
        return CuratorOutput(ops=self.ops)


def session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session
    return factory


@pytest_asyncio.fixture
async def world(db_session, test_user):
    """Real FalkorDB graph for test_user, kept in sync with the test ledger."""
    memory = FalkorGraphMemory(host=FALKOR_HOST, port=FALKOR_PORT, graph_prefix="test_trader_")
    if not await memory.ping():
        await memory.close()
        pytest.skip(f"no FalkorDB at {FALKOR_HOST}:{FALKOR_PORT} (docker compose up -d falkordb)")
    await memory.delete_trader_graph(test_user.id)
    sync = GraphSync(memory, session_factory(db_session), retry_delay=0)
    ledger_events.subscribe(sync.on_ledger_changed)

    class World:
        user_id = test_user.id
        db = db_session
        mem = memory

        def curator(self, model=None):
            return Curator(memory, session_factory(db_session),
                           model_factory=(lambda: model) if model else None)

        async def record(self, *entries):
            result = await persist_transactions(db_session, list(entries), test_user.id)
            await ledger_events.drain()
            return result["logged"]

        async def rows(self, cypher, params=None):
            graph = (await memory._client()).select_graph(memory.graph_name(test_user.id))
            return (await graph.query(cypher, params or {})).result_set

        async def turn(self, message, touched=(), today=date(2026, 9, 9), history=(), turn_id=101):
            """A turn as respond() builds it, including real recall from the graph."""
            return Turn(user_id=test_user.id, turn_id=turn_id, message=message, reply="ok", touched=list(touched),
                        today=today, history=list(history), recalled=await recall(memory, test_user.id, message))

    yield World()
    ledger_events.unsubscribe(sync.on_ledger_changed)
    await memory.delete_trader_graph(test_user.id)
    await memory.close()


def credit_sale(party, amount, due=None, day=date(2026, 9, 2), item="beans", qty=2, unit="bags", paid=0):
    return TransactionInput(type="sale", party=party, item=item, quantity=qty, unit=unit, amount=amount,
                            amount_paid=paid, direction="in", date=day, due_date=due)


async def test_a_due_date_becomes_a_promise_learned_from_the_message(world):
    logged = await world.record(credit_sale("Emeka", 60000, due=date(2026, 9, 7)))
    outcome = await world.curator().curate(await world.turn("Emeka collect 2 bags beans, 60k. He go pay Monday",
                                                      touched=logged, today=date(2026, 9, 2)))

    assert outcome.promises == 1 and outcome.asked_model is False  # no model configured
    assert await world.rows("MATCH (pr:Promise)-[:MADE_BY]->(p:Party), (pr)-[:LEARNED_FROM]->(m:Message), "
                            "(pr)-[:WRITTEN_BY]->(a:Agent) RETURN p.key, pr.due, pr.status, m.turn_id, a.name") \
        == [["emeka", "2026-09-07", "open", 101, "curator"]]


async def test_more_time_after_the_due_date_breaks_the_old_promise(world):
    [entry] = await world.record(credit_sale("Emeka", 60000, due=date(2026, 9, 7)))
    curator = world.curator()
    await curator.curate(await world.turn("he go pay Monday", touched=[entry], today=date(2026, 9, 2)))

    updated = (await correct_transaction(world.db, world.user_id, entry["id"], due_date=date(2026, 9, 11)))["updated"]
    await ledger_events.drain()
    await curator.curate(await world.turn("Emeka beg make I give am till Friday", touched=[updated],
                                    today=date(2026, 9, 9), turn_id=102))

    assert await world.rows("MATCH (pr:Promise) RETURN pr.due, pr.status, pr.paid_fraction ORDER BY pr.due") \
        == [["2026-09-07", "broken", 0.0], ["2026-09-11", "open", None]]


async def test_more_time_before_the_due_date_replaces_it_without_blame(world):
    [entry] = await world.record(credit_sale("Iya Toyin", 60000, due=date(2026, 9, 15)))
    curator = world.curator()
    await curator.curate(await world.turn("next week Tuesday", touched=[entry], today=date(2026, 9, 8)))
    updated = (await correct_transaction(world.db, world.user_id, entry["id"], due_date=date(2026, 9, 18)))["updated"]
    await ledger_events.drain()
    await curator.curate(await world.turn("she say make I give am till Friday", touched=[updated],
                                    today=date(2026, 9, 10), turn_id=102))

    assert await world.rows("MATCH (pr:Promise) RETURN pr.due, pr.status ORDER BY pr.due") \
        == [["2026-09-15", "replaced"], ["2026-09-18", "open"]]


async def test_paying_by_the_due_date_keeps_the_promise(world):
    [entry] = await world.record(credit_sale("Mama Chidi", 45000, due=date(2026, 9, 4), day=date(2026, 9, 3)))
    await world.curator().curate(await world.turn("she go pay Friday", touched=[entry], today=date(2026, 9, 3)))

    await world.record(TransactionInput(type="debt_repayment", party="Mama Chidi", amount=45000,
                                        direction="in", date=date(2026, 9, 4)))

    assert await world.rows("MATCH (pr:Promise) RETURN pr.status, pr.closed_on") == [["kept", "2026-09-04"]]


async def test_paying_late_does_not_keep_the_promise(world):
    [entry] = await world.record(credit_sale("Emeka", 36000, due=date(2026, 9, 24), day=date(2026, 9, 21)))
    await world.curator().curate(await world.turn("he go pay Thursday", touched=[entry], today=date(2026, 9, 21)))
    await world.record(TransactionInput(type="debt_repayment", party="Emeka", amount=36000, direction="in",
                                        date=date(2026, 9, 30)))
    assert await world.rows("MATCH (pr:Promise) RETURN pr.status") == [["open"]]  # the Collector marks it broken


async def test_three_identical_sales_make_an_inferred_usual_order(world):
    curator = world.curator()
    for day in (5, 12):
        logged = await world.record(credit_sale("Bola", 90000, item="rice", day=date(2026, 9, day), paid=90000))
        await curator.curate(await world.turn("Bola buy 2 bags rice", touched=logged))
    assert await world.rows("MATCH (f:Fact) RETURN count(f)") == [[0]]  # two isn't a habit

    logged = await world.record(credit_sale("Bola", 90000, item="rice", day=date(2026, 9, 19), paid=90000))
    outcome = await curator.curate(await world.turn("Bola buy 2 bags rice", touched=logged))

    assert outcome.inferred == 1
    [[summary, confidence, data]] = await world.rows("MATCH (f:Fact {kind:'usual_order'}) RETURN f.summary, "
                                                     "f.confidence, f.data")
    assert (summary, confidence) == ("2 bags rice", "inferred") and '"payment": "cash"' in data


async def test_what_the_trader_said_beats_an_inferred_usual_order(world):
    curator = world.curator(ScriptedModel([MemoryOp(op="set_usual_order", party="Bola",
                                                    items=[OrderItem(item="rice", quantity=1, unit="bag")],
                                                    said="Bola usual na 1 bag")]))
    logged = await world.record(credit_sale("Bola", 45000, item="rice", qty=1, unit="bag", paid=45000))
    await curator.curate(await world.turn("Bola usual na 1 bag rice", touched=logged))
    for day in (12, 19, 26):
        logged = await world.record(credit_sale("Bola", 90000, item="rice", day=date(2026, 9, day), paid=90000))
        await world.curator().curate(await world.turn("Bola buy 2 bags rice", touched=logged))

    assert await world.rows("MATCH (f:Fact {kind:'usual_order'}) WHERE f.valid_to IS NULL "
                            "RETURN f.summary, f.confidence") == [["1 bag rice", "stated"]]


async def test_alias_confirmed_by_the_trader(world):
    await world.record(credit_sale("Mama Chidi", 45000), credit_sale("Chidi", 2000))
    model = ScriptedModel([MemoryOp(op="add_alias", party="Mama Chidi", alias="Chidi's mum")])
    history = [{"role": "user", "content": "Chidi's mum take 2 bags garri, 30k"},
               {"role": "assistant", "content": "Is Chidi's mum the same as Mama Chidi?"}]
    outcome = await world.curator(model).curate(await world.turn("yes", history=history))

    assert outcome.applied == ["add_alias"]
    assert await world.rows("MATCH (a:Alias)-[:REFERS_TO]->(p:Party) RETURN a.text, a.kind, p.key") \
        == [["Chidi's mum", "nickname", "mama chidi"]]
    assert "Is Chidi's mum the same as Mama Chidi?" in model.calls[0]  # the model saw the question


async def test_alias_rules(world):
    await world.record(credit_sale("Mama Chidi", 45000), credit_sale("Chidi", 2000))
    model = ScriptedModel([
        MemoryOp(op="add_alias", party="Mama Chidi", alias="Chidi"),        # Chidi has his own records
        MemoryOp(op="add_alias", party="Mama Chidi", alias="Mummy C"),      # never said
        MemoryOp(op="add_alias", party="Mama Ngozi", alias="Mama C"),       # unknown person
    ])
    outcome = await world.curator(model).curate(await world.turn("Chidi and Mama C came today"))

    assert outcome.applied == []
    assert [r for _, r in outcome.rejected] == ["a separate person with their own records",
                                                "alias not in the conversation", "unknown person"]
    assert await world.rows("MATCH (a:Alias) RETURN count(a)") == [[0]]


async def test_a_misheard_name_is_folded_into_the_real_one(world):
    [bolu] = await world.record(credit_sale("Bolu", 90000, item="rice", paid=90000))
    await correct_transaction(world.db, world.user_id, bolu["id"], party="Bola")
    await ledger_events.drain()  # Bolu now has no entries, so the sync leaves an empty node
    model = ScriptedModel([MemoryOp(op="add_alias", party="Bola", alias="Bolu")])
    outcome = await world.curator(model).curate(await world.turn("Bolu na Bola, the voice thing spell am wrong"))

    assert outcome.applied == ["add_alias"]
    assert await world.rows("MATCH (p:Party) RETURN p.key") == [["bola"]]
    assert await world.rows("MATCH (a:Alias) RETURN a.text, a.kind") == [["Bolu", "transcription"]]


async def test_do_not_remind_and_lifting_it(world):
    await world.record(credit_sale("Pastor John", 30000))
    said = "No remind Pastor John o, I go talk to am myself"
    await world.curator(ScriptedModel([MemoryOp(op="set_do_not_remind", party="Pastor John", said=said)])) \
        .curate(await world.turn(said))
    [memory] = await world.mem.recall(world.user_id, ["pastor john"])
    assert [(f.kind, f.said) for f in memory.facts] == [("do_not_remind", said)]

    await world.curator(ScriptedModel([MemoryOp(op="lift_do_not_remind", party="Pastor John")])) \
        .curate(await world.turn("you fit remind Pastor John now", turn_id=102))
    [memory] = await world.mem.recall(world.user_id, ["pastor john"])
    assert memory.facts == ()


async def test_a_customer_becomes_a_supplier(world):
    await world.record(credit_sale("Alhaji Musa", 75000))
    model = ScriptedModel([MemoryOp(op="set_role", party="Alhaji Musa", role="customer", active=False),
                           MemoryOp(op="set_role", party="Alhaji Musa", role="supplier", active=True)])
    await world.curator(model).curate(await world.turn("Alhaji Musa no dey buy from me again, na him dey supply my oil"))
    await world.record(credit_sale("Alhaji Musa", 1000))  # a later sync must not bring "customer" back

    [memory] = await world.mem.recall(world.user_id, ["alhaji musa"])
    assert memory.roles == ("supplier",)


async def test_no_model_call_when_nothing_could_be_remembered(world):
    model = ScriptedModel()
    outcome = await world.curator(model).curate(await world.turn("who owe me?"))
    assert outcome.asked_model is False and model.calls == []


async def test_a_failing_model_is_logged_not_raised(world):
    await world.record(credit_sale("Emeka", 1000))
    outcome = await world.curator(ScriptedModel(fail=True)).curate(await world.turn("Emeka is my guy"))
    assert outcome.asked_model and outcome.applied == [] and outcome.rejected == []


async def test_whole_turn_through_the_assistant(world, monkeypatch):
    """respond(): the Recorder records via its tool, the reply is saved, and the curator is
    handed a turn that turns the due date into a promise learned from that message.

    In production the sync and curator run in the background on their own DB sessions.
    Tests share one session, so here the turn is captured and run after respond() returns."""
    from db import ConversationTurn
    from memory import set_curator, set_graph_memory
    from memory.null import NullGraphMemory
    from sqlalchemy import select
    from tools import assistant, router

    async def recorder(text, prompt, tools, history=None, callbacks=None):
        extract = next(t for t in tools if t.name == "extract_transactions_tool")
        await extract.ainvoke({"transactions": [{"type": "sale", "party": "Emeka", "item": "beans", "amount": 60000,
                                                 "amount_paid": 0, "direction": "in", "due_date": "2026-09-07"}]})
        return {"messages": [AIMessage(content="Recorded.")]}

    events, turns = [], []

    async def hold(event):
        events.append(event)

    monkeypatch.setattr(router, "handle_message", recorder)
    curator = world.curator()
    monkeypatch.setattr(curator, "schedule", turns.append)
    ledger_events.subscribe(hold)
    set_graph_memory(world.mem)
    set_curator(curator)
    try:
        sync_handlers = [h for h in ledger_events._handlers if h is not hold]
        for h in sync_handlers:
            ledger_events.unsubscribe(h)
        await assistant.respond(world.db, world.user_id, "Emeka collect 2 bags beans, 60k. He go pay Monday", "web")
        for h in sync_handlers:
            ledger_events.subscribe(h)
            for event in events:
                await h(event)
        [turn] = turns
        await curator.curate(turn)
    finally:
        ledger_events.unsubscribe(hold)
        set_graph_memory(NullGraphMemory())
        set_curator(None)

    turn_id = (await world.db.execute(select(ConversationTurn.id).where(ConversationTurn.role == "user"))).scalar()
    assert turn.turn_id == turn_id and [e["party"] for e in turn.touched] == ["Emeka"]
    assert await world.rows("MATCH (pr:Promise)-[:LEARNED_FROM]->(m:Message) RETURN pr.due, m.turn_id")         == [["2026-09-07", turn_id]]


async def test_recall_counts_open_debts(world):
    """Regression: FalkorDB drops filtered OPTIONAL MATCHes after an aggregation, which
    once made every open-debt count 0."""
    await world.record(credit_sale("Emeka", 60000), credit_sale("Emeka", 45000, paid=45000))
    [memory] = await world.mem.recall(world.user_id, ["emeka"])
    assert memory.open_debts == 1


# ── the record tool refuses a new name that may be someone already known ─────

async def test_record_tool_asks_before_splitting_a_customer(db_session, test_user):
    from tools.clean_version import build_tools

    recalled = Recalled(possible={"chidi": ["Chidi", "Mama Chidi"]}, known_keys=frozenset({"chidi", "mama chidi"}),
                        strong=frozenset({"chidi"}))
    tools = {t.name: t for t in build_tools(db_session, test_user.id, [], could_be=recalled.could_be)}
    extract = tools["extract_transactions_tool"]
    sale = {"type": "sale", "party": "Chidi's mum", "item": "garri", "amount": 30000, "amount_paid": 0,
            "direction": "in"}

    # the model can't confirm a name the user was never asked about
    self_confirmed = await extract.ainvoke({"transactions": [sale], "confirmed_new_names": ["Chidi's mum"]})
    assert "could be Chidi or Mama Chidi" in self_confirmed["error"]
    tools = {t.name: t for t in build_tools(db_session, test_user.id, [], could_be=recalled.could_be)}
    extract = tools["extract_transactions_tool"]
    refused = await extract.ainvoke({"transactions": [sale]})
    assert "could be Chidi or Mama Chidi" in refused["error"] and "Nothing recorded" in refused["error"]

    # having been refused, the agent can't just pick one of the candidates itself...
    guessed = await extract.ainvoke({"transactions": [{**sale, "party": "Mama Chidi"}]})
    assert "hasn't said whether" in guessed["error"]
    # ...but once the user confirms who it is (a new request), it records
    asked = "Is Chidi's mum the same as Chidi or Mama Chidi, or someone new?"
    fresh = {t.name: t for t in build_tools(db_session, test_user.id, [], could_be=recalled.could_be,
                                            last_reply=asked)}
    as_known = await fresh["extract_transactions_tool"].ainvoke({"transactions": [{**sale, "party": "Mama Chidi"}]})
    assert as_known["logged"][0]["party"] == "Mama Chidi"
    as_new = await fresh["extract_transactions_tool"].ainvoke({"transactions": [sale],
                                                               "confirmed_new_names": ["Chidi's mum"]})
    assert as_new["logged"][0]["party"] == "Chidi's mum"


async def test_a_new_person_sharing_a_word_with_someone_known_is_not_blocked(db_session, test_user):
    """"I pay Chidi 2k" when only Mama Chidi is known: Chidi is someone else. A shared word
    is a hint for the agent, never a refusal."""
    from tools.clean_version import build_tools

    _, possible, _, strong = match_names("I pay Chidi 2k to carry the bags", known("Mama Chidi"))
    recalled = Recalled(possible={k: sorted(v) for k, v in possible.items()}, known_keys=frozenset({"mama chidi"}),
                        strong=frozenset(strong))
    assert recalled.possible == {"chidi": ["Mama Chidi"]} and recalled.could_be("Chidi") == []
    extract = {t.name: t for t in build_tools(db_session, test_user.id, [], could_be=recalled.could_be)}[
        "extract_transactions_tool"]
    result = await extract.ainvoke({"transactions": [{"type": "expense", "party": "Chidi", "item": "carrying",
                                                      "amount": 2000, "direction": "out"}]})
    assert result["logged"][0]["party"] == "Chidi"


def test_a_possessive_touching_a_known_name_is_strong():
    """"Chidi's mum" when only Mama Chidi is known: a reference to someone, so ask."""
    _, possible, _, strong = match_names("Chidi's mum take 2 bags garri", known("Mama Chidi"))
    recalled = Recalled(possible={k: sorted(v) for k, v in possible.items()}, known_keys=frozenset({"mama chidi"}),
                        strong=frozenset(strong))
    assert recalled.could_be("Chidi's mum") == ["Mama Chidi"]


async def test_restating_a_do_not_remind_writes_nothing_new(world):
    await world.record(credit_sale("Pastor John", 30000))
    op = MemoryOp(op="set_do_not_remind", party="Pastor John", said="no remind am")
    await world.curator(ScriptedModel([op])).curate(await world.turn("No remind Pastor John"))
    again = await world.curator(ScriptedModel([op])).curate(await world.turn("remind Pastor John", turn_id=102))
    assert again.unchanged == ["set_do_not_remind"] and again.applied == []
    assert await world.rows("MATCH (f:Fact {kind:'do_not_remind'}) RETURN count(f)") == [[1]]


async def test_a_name_with_ledger_entries_is_never_absorbed_as_an_alias(world):
    """Chidi the porter has only a cash expense (no graph debt), so his node looks empty to
    the graph. The ledger still says he's a separate person."""
    await world.record(credit_sale("Mama Chidi", 45000),
                       TransactionInput(type="expense", party="Chidi", item="carrying", amount=2000, direction="out"))
    model = ScriptedModel([MemoryOp(op="add_alias", party="Mama Chidi", alias="Chidi")])
    outcome = await world.curator(model).curate(await world.turn("na Chidi carry am for Mama Chidi"))

    assert outcome.rejected == [("add_alias", "a separate person with their own records")]
    assert sorted(await world.rows("MATCH (p:Party) RETURN p.key")) == [["chidi"], ["mama chidi"]]


async def test_an_empty_answer_in_the_wrong_shape_means_nothing_to_remember(world):
    """Seen live on gpt-oss-20b: Groq rejects a bare [] with json_validate_failed."""
    class SaysEmptyList(ScriptedModel):
        async def ainvoke(self, messages, config=None):
            raise RuntimeError("Error code: 400 - {'error': {'code': 'json_validate_failed', "
                               "'failed_generation': '[]'}}")

    await world.record(credit_sale("Emeka", 1000))
    outcome = await world.curator(SaysEmptyList()).curate(await world.turn("Emeka is a good customer"))
    assert outcome.asked_model and outcome.applied == [] and outcome.rejected == []


async def test_a_host_without_json_schema_output_falls_back_to_tool_calling(world):
    """Not every host behind OpenRouter takes JSON-schema output for every model."""
    class NoJsonSchema(ScriptedModel):
        def with_structured_output(self, schema, **kwargs):
            if kwargs.get("method") == "json_schema":
                class Refuses:
                    async def ainvoke(self, messages, config=None):
                        raise RuntimeError("Error code: 400 - response_format json_schema is not supported")
                return Refuses()
            return self

    await world.record(credit_sale("Pastor John", 30000))
    op = MemoryOp(op="set_do_not_remind", party="Pastor John", said="no remind am")
    outcome = await world.curator(NoJsonSchema([op])).curate(await world.turn("No remind Pastor John"))
    assert outcome.applied == ["set_do_not_remind"]

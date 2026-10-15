"""Graph memory, Day 1: the ledger announces changes, the sync turns the ledger into party
snapshots, and the FalkorDB adapter mirrors them.

The FalkorDB tests run against a real FalkorDB (docker compose up -d falkordb) and are
skipped if none is reachable. They use their own graphs (test_trader_<id>) and delete them.
"""
import os
from contextlib import asynccontextmanager
from datetime import date

import pytest
import pytest_asyncio

from memory.falkor import FalkorGraphMemory
from memory.models import DebtSnapshot, GraphMemoryError, PartySnapshot
from memory.sync import GraphSync, party_snapshots
from tools.ledger import TransactionInput, correct_transaction, delete_transactions, persist_transactions
from tools.ledger_events import LedgerChanged, ledger_events


def sale(party, amount, paid=None, day=date(2026, 9, 2), due=None, item="rice"):
    return TransactionInput(type="sale", party=party, item=item, amount=amount, amount_paid=paid,
                            direction="in", date=day, due_date=due)


def repayment(party, amount, day=date(2026, 9, 4)):
    return TransactionInput(type="debt_repayment", party=party, amount=amount, direction="in", date=day)


@pytest_asyncio.fixture
async def events():
    """Every LedgerChanged published during the test."""
    seen: list[LedgerChanged] = []

    async def record(event):
        seen.append(event)

    ledger_events.subscribe(record)
    yield seen
    ledger_events.unsubscribe(record)


# ── the ledger announces what changed ────────────────────────────────────────

async def test_recording_announces_the_parties(db_session, test_user, events):
    await persist_transactions(db_session, [sale("Mama Chidi ", 45000, paid=0), sale("Emeka", 60000)], test_user.id)
    await ledger_events.drain()

    assert len(events) == 1
    assert events[0].party_keys == {"mama chidi", "emeka"}
    assert events[0].reason == "recorded" and events[0].user_id == test_user.id


async def test_correcting_the_party_announces_old_and_new(db_session, test_user, events):
    result = await persist_transactions(db_session, [sale("Bolu", 90000)], test_user.id)
    await correct_transaction(db_session, test_user.id, result["logged"][0]["id"], party="Bola")
    await ledger_events.drain()

    assert events[-1].party_keys == {"bolu", "bola"} and events[-1].reason == "corrected"


async def test_undo_announces_the_party(db_session, test_user, events):
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0)], test_user.id)
    await delete_transactions(db_session, test_user.id)
    await ledger_events.drain()

    assert events[-1].party_keys == {"emeka"} and events[-1].reason == "deleted"


async def test_no_event_for_entries_without_a_party(db_session, test_user, events):
    await persist_transactions(db_session, [TransactionInput(type="expense", item="fuel", amount=2000,
                                                             direction="out")], test_user.id)
    await ledger_events.drain()
    assert events == []


async def test_a_failing_listener_never_breaks_the_ledger(db_session, test_user):
    async def broken(_):
        raise RuntimeError("graph exploded")

    ledger_events.subscribe(broken)
    try:
        result = await persist_transactions(db_session, [sale("Emeka", 60000, paid=0)], test_user.id)
        await ledger_events.drain()
    finally:
        ledger_events.unsubscribe(broken)
    assert result["logged"][0]["outstanding"] == 60000


# ── ledger → snapshots ───────────────────────────────────────────────────────

async def test_snapshots_only_count_credit_as_debts(db_session, test_user):
    await persist_transactions(db_session, [
        sale("Bola", 90000),                                   # cash: not a debt
        sale("Mama Chidi", 45000, paid=0, due=date(2026, 9, 4)),
        TransactionInput(type="expense", party="Alhaji Musa", item="palm oil", amount=120000,
                         amount_paid=70000, direction="out", date=date(2026, 9, 16)),
    ], test_user.id)

    snaps = {s.key: s for s in await party_snapshots(db_session, test_user.id)}

    assert snaps["bola"].debts == () and snaps["bola"].customer_since == date(2026, 9, 2)
    [debt] = snaps["mama chidi"].debts
    assert (debt.kind, debt.open, debt.due) == ("receivable", True, date(2026, 9, 4))
    [owed] = snaps["alhaji musa"].debts
    assert (owed.kind, owed.open) == ("payable", True)
    assert snaps["alhaji musa"].customer_since is None  # buying from someone doesn't make them a customer


async def test_a_repaid_debt_stays_as_a_closed_debt(db_session, test_user):
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0)], test_user.id)
    await persist_transactions(db_session, [repayment("Emeka", 60000)], test_user.id)

    [snap] = await party_snapshots(db_session, test_user.id, {"emeka"})
    [debt] = snap.debts
    assert debt.open is False  # kept: whether he paid on time is promise history


async def test_snapshot_for_a_fully_undone_party_has_no_name(db_session, test_user):
    await persist_transactions(db_session, [sale("Bolu", 90000, paid=0)], test_user.id)
    await delete_transactions(db_session, test_user.id)

    assert await party_snapshots(db_session, test_user.id, {"bolu"}) == [PartySnapshot(key="bolu", name=None)]


async def test_snapshots_use_the_latest_spelling_and_stay_per_trader(db_session, test_user, other_user):
    await persist_transactions(db_session, [sale("mama chidi", 1000, paid=0, day=date(2026, 9, 1))], test_user.id)
    await persist_transactions(db_session, [sale("Mama Chidi", 2000, paid=0, day=date(2026, 9, 3))], test_user.id)
    await persist_transactions(db_session, [sale("Mama Chidi", 5000, paid=0)], other_user.id)

    [snap] = await party_snapshots(db_session, test_user.id)
    assert snap.name == "Mama Chidi" and len(snap.debts) == 2


# ── sync service ─────────────────────────────────────────────────────────────

class FakeMemory:
    enabled = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def sync_parties(self, user_id, parties):
        if self.fail:
            raise GraphMemoryError("down")
        self.calls.append((user_id, parties))


def session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session
    return factory


async def test_sync_sends_snapshots_of_the_changed_parties(db_session, test_user):
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0), sale("Bola", 90000)], test_user.id)
    memory = FakeMemory()

    await GraphSync(memory, session_factory(db_session)).on_ledger_changed(
        LedgerChanged(user_id=test_user.id, party_keys=frozenset({"emeka"}), reason="recorded"))

    [(user_id, parties)] = memory.calls
    assert user_id == test_user.id and [p.key for p in parties] == ["emeka"]


async def test_sync_swallows_graph_failures(db_session, test_user):
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0)], test_user.id)
    memory = FakeMemory(fail=True)
    sync = GraphSync(memory, session_factory(db_session), retry_delay=0)

    # must not raise: the ledger has committed, the graph catches up later
    await sync.on_ledger_changed(LedgerChanged(user_id=test_user.id, party_keys=frozenset({"emeka"}),
                                               reason="recorded"))


async def test_sync_retries_once_after_a_blip(db_session, test_user):
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0)], test_user.id)

    class FlakyOnce(FakeMemory):
        failures = 1

        async def sync_parties(self, user_id, parties):
            if self.failures:
                self.failures -= 1
                raise GraphMemoryError("timeout")
            self.calls.append((user_id, parties))

    memory = FlakyOnce()
    await GraphSync(memory, session_factory(db_session), retry_delay=0).on_ledger_changed(
        LedgerChanged(user_id=test_user.id, party_keys=frozenset({"emeka"}), reason="recorded"))
    assert len(memory.calls) == 1


# ── FalkorDB adapter (real engine) ───────────────────────────────────────────

FALKOR_HOST = os.getenv("FALKORDB_TEST_HOST", "127.0.0.1")
FALKOR_PORT = int(os.getenv("FALKORDB_TEST_PORT", "6379"))
T1, T2 = 900001, 900002  # trader ids used only by these tests


@pytest_asyncio.fixture
async def falkor():
    memory = FalkorGraphMemory(host=FALKOR_HOST, port=FALKOR_PORT, graph_prefix="test_trader_")
    if not await memory.ping():
        await memory.close()
        pytest.skip(f"no FalkorDB at {FALKOR_HOST}:{FALKOR_PORT} (docker compose up -d falkordb)")
    for t in (T1, T2):
        await memory.delete_trader_graph(t)
    yield memory
    for t in (T1, T2):
        await memory.delete_trader_graph(t)
    await memory.close()


async def rows(memory, user_id, cypher, params=None):
    graph = (await memory._client()).select_graph(memory.graph_name(user_id))
    return (await graph.query(cypher, params or {})).result_set


def debt(pg_id, open=True, kind="receivable", due=None):
    return DebtSnapshot(pg_id=pg_id, kind=kind, open=open, date=date(2026, 9, 2), due=due)


async def test_falkor_mirrors_parties_debts_and_customer_role(falkor):
    snap = PartySnapshot(key="emeka", name="Emeka", debts=(debt(1, due=date(2026, 9, 7)), debt(2, open=False)),
                         customer_since=date(2026, 9, 2))
    await falkor.sync_parties(T1, [snap])
    await falkor.sync_parties(T1, [snap])  # idempotent

    assert await rows(falkor, T1, "MATCH (p:Party) RETURN p.key, p.name") == [["emeka", "Emeka"]]
    assert await rows(falkor, T1, "MATCH (d:Debt)-[:WITH]->(:Party {key:'emeka'}) "
                                  "RETURN d.pg_id, d.open, d.due ORDER BY d.pg_id") == [[1, True, "2026-09-07"],
                                                                                         [2, False, None]]
    assert await rows(falkor, T1, "MATCH (:Party {key:'emeka'})-[r:CUSTOMER_OF]->(:Trader {id:$t}) "
                                  "RETURN r.confidence, r.valid_from", {"t": T1}) == [["inferred", "2026-09-02"]]


async def test_falkor_moves_a_debt_when_a_correction_changes_the_party(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="bolu", name="Bolu", debts=(debt(7),))])
    await falkor.sync_parties(T1, [PartySnapshot(key="bolu", name=None),
                                   PartySnapshot(key="bola", name="Bola", debts=(debt(7),))])

    assert await rows(falkor, T1, "MATCH (d:Debt {pg_id: 7})-[:WITH]->(p) RETURN p.key") == [["bola"]]
    assert await rows(falkor, T1, "MATCH (d:Debt) RETURN count(d)") == [[1]]


async def test_falkor_removes_undone_debts_and_their_promises(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="emeka", name="Emeka", debts=(debt(1), debt(2)))])
    await rows(falkor, T1, "MATCH (d:Debt {pg_id: 2}), (p:Party {key:'emeka'}) "
                           "CREATE (:Promise {due:'2026-09-11', status:'open'})-[:FOR]->(d)")

    await falkor.sync_parties(T1, [PartySnapshot(key="emeka", name="Emeka", debts=(debt(1),))])

    assert await rows(falkor, T1, "MATCH (d:Debt) RETURN d.pg_id") == [[1]]
    assert await rows(falkor, T1, "MATCH (pr:Promise) RETURN count(pr)") == [[0]]


async def test_falkor_never_overrides_a_stated_role(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="alhaji musa", name="Alhaji Musa",
                                                 customer_since=date(2026, 9, 7))])
    # the curator recorded "Musa no dey buy from me again": inferred role replaced by a closed stated one
    await rows(falkor, T1, "MATCH (p:Party {key:'alhaji musa'})-[r:CUSTOMER_OF]->(t:Trader) DELETE r "
                           "WITH p, t CREATE (p)-[:CUSTOMER_OF {confidence:'stated', valid_from:'2026-09-07', "
                           "valid_to:'2026-09-16'}]->(t)")

    await falkor.sync_parties(T1, [PartySnapshot(key="alhaji musa", name="Alhaji Musa",
                                                 customer_since=date(2026, 9, 7))])

    assert await rows(falkor, T1, "MATCH (:Party)-[r:CUSTOMER_OF]->() RETURN r.confidence") == [["stated"]]


async def test_falkor_keeps_each_trader_in_their_own_graph(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="emeka", name="Emeka")])
    await falkor.sync_parties(T2, [PartySnapshot(key="bola", name="Bola")])

    assert await rows(falkor, T1, "MATCH (p:Party) RETURN p.key") == [["emeka"]]
    assert await rows(falkor, T2, "MATCH (p:Party) RETURN p.key") == [["bola"]]


async def test_falkor_undone_party_gets_no_node(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="typo", name=None)])
    assert await rows(falkor, T1, "MATCH (p:Party) RETURN count(p)") == [[0]]


async def test_falkor_prune_removes_debts_not_in_the_ledger(falkor):
    await falkor.sync_parties(T1, [PartySnapshot(key="emeka", name="Emeka", debts=(debt(1), debt(2), debt(3)))])
    assert await falkor.prune_debts(T1, [1, 3]) == 1
    assert await rows(falkor, T1, "MATCH (d:Debt) RETURN d.pg_id ORDER BY d.pg_id") == [[1], [3]]


async def test_falkor_unreachable_raises_graph_memory_error():
    memory = FalkorGraphMemory(host="127.0.0.1", port=1, timeout_seconds=0.5, graph_prefix="test_trader_")
    try:
        assert await memory.ping() is False
        with pytest.raises(GraphMemoryError):
            await memory.sync_parties(T1, [PartySnapshot(key="emeka", name="Emeka")])
    finally:
        await memory.close()


async def test_full_rebuild_from_the_ledger(db_session, test_user, falkor, monkeypatch):
    monkeypatch.setattr(falkor, "graph_name", lambda user_id: f"test_trader_{T1}")
    await persist_transactions(db_session, [sale("Emeka", 60000, paid=0), sale("Bola", 90000)], test_user.id)
    await falkor.sync_parties(test_user.id, [PartySnapshot(key="ghost", name="Ghost", debts=(debt(999999),))])

    summary = await GraphSync(falkor, session_factory(db_session)).rebuild(test_user.id)

    assert summary["parties"] == 2 and summary["debts"] == 1 and summary["removed"] == 1
    assert await rows(falkor, T1, "MATCH (p:Party) RETURN p.key ORDER BY p.key") == [["bola"], ["emeka"], ["ghost"]]
    assert await rows(falkor, T1, "MATCH (d:Debt)-[:WITH]->(p) RETURN p.key") == [["emeka"]]


async def test_reads_are_retried_once_and_writes_never():
    """A dropped connection fails one query; a read just tries again on a fresh one."""
    from redis.exceptions import TimeoutError as RedisTimeout

    class FlakyGraph:
        name = "test_trader_x"

        def __init__(self):
            self.reads = self.writes = 0

        async def ro_query(self, cypher, params=None, timeout=None):
            self.reads += 1
            if self.reads == 1:
                raise RedisTimeout("Timeout reading from 127.0.0.1:6379")
            return "ok"

        async def query(self, cypher, params=None, timeout=None):
            self.writes += 1
            raise RedisTimeout("Timeout reading from 127.0.0.1:6379")

    memory = FalkorGraphMemory(graph_prefix="test_trader_")
    graph = FlakyGraph()
    assert await memory._run(graph, "MATCH (n) RETURN n", {}, "read", read_only=True) == "ok"
    assert graph.reads == 2
    with pytest.raises(GraphMemoryError):
        await memory._run(graph, "CREATE (n)", {}, "write")
    assert graph.writes == 1

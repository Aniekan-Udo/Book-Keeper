"""FalkorDB adapter for the GraphMemory port.

One graph per trader (`trader_<user_id>`): a trader's agents can never read another
trader's memory, even through a buggy query, and queries need no trader filter.

Every Cypher statement the product runs lives in this file as a constant, with parameters
(never string-built), so the full list can be reviewed in one place.
"""
import asyncio
import time
import uuid
from datetime import date, datetime, timezone

from falkordb.asyncio import FalkorDB
from redis.exceptions import RedisError, ResponseError

from config import business_today
from memory.models import (
    Explanation, FactView, GraphMemoryError, KnownName, OverduePromise, PartyMemory, PartySnapshot, PromiseView,
    Source,
)
from utils import logger

# ── schema ───────────────────────────────────────────────────────────────────
# (label, property, unique)
_INDEXES = (
    ("Party", "key", True),
    ("Debt", "pg_id", True),
    ("Alias", "key", False),
    ("Promise", "id", True),
    ("Message", "turn_id", True),
    ("Agent", "name", True),
    ("Reminder", "pg_id", True),
)

MERGE_TRADER = "MERGE (:Trader {id: $user_id})"

# ── ledger sync ──────────────────────────────────────────────────────────────
UPSERT_PARTY = """
MERGE (t:Trader {id: $user_id})
MERGE (p:Party {key: $key})
ON CREATE SET p.created_at = $now
SET p.name = $name
"""

# Debt nodes are keyed by the Postgres id. A correction can move an entry to another
# party, so any WITH edge to a different party is dropped before linking to this one.
UPSERT_DEBTS = """
MATCH (p:Party {key: $key})
UNWIND $debts AS d
MERGE (n:Debt {pg_id: d.pg_id})
SET n.kind = d.kind, n.open = d.open, n.date = d.date, n.due = d.due, n.settled_on = d.settled_on
WITH p, n
OPTIONAL MATCH (n)-[w:WITH]->(other:Party)
WHERE other.key <> p.key
DELETE w
WITH DISTINCT p, n
MERGE (n)-[:WITH]->(p)
"""

STATED_CUSTOMER_ROLES = """
MATCH (:Party {key: $key})-[r:CUSTOMER_OF {confidence: 'stated'}]->(:Trader)
RETURN count(r)
"""

SET_INFERRED_CUSTOMER = """
MATCH (t:Trader {id: $user_id}), (p:Party {key: $key})
MERGE (p)-[r:CUSTOMER_OF {confidence: 'inferred'}]->(t)
SET r.valid_from = $since
"""

DROP_INFERRED_CUSTOMER = """
MATCH (:Party {key: $key})-[r:CUSTOMER_OF {confidence: 'inferred'}]->(:Trader)
DELETE r
"""

# Debts still linked to these parties that the ledger no longer has (undone entries).
# A promise about a debt that no longer exists means nothing, so it goes too.
REMOVE_STALE_DEBTS = """
MATCH (n:Debt)-[:WITH]->(p:Party)
WHERE p.key IN $keys AND NOT n.pg_id IN $alive
OPTIONAL MATCH (pr:Promise)-[:FOR]->(n)
WITH collect(DISTINCT n) + collect(DISTINCT pr) AS doomed
UNWIND doomed AS x
DETACH DELETE x
RETURN count(x)
"""

# A promise is kept when its debt was cleared (dated by the payment that cleared it, from
# the ledger) on or before the due date. Runs as part of every sync, so it doesn't matter
# which agent recorded the payment. A late payment leaves it open for the Collector to
# mark broken.
MARK_KEPT = """
MATCH (pr:Promise {status: 'open'})-[:FOR]->(d:Debt {open: false})-[:WITH]->(p:Party)
WHERE p.key IN $keys AND d.settled_on IS NOT NULL AND pr.due >= d.settled_on
SET pr.status = 'kept', pr.paid_fraction = 1.0, pr.closed_on = d.settled_on, pr.closed_by = 'ledger_sync'
RETURN count(pr)
"""

PRUNE_DEBTS = """
MATCH (n:Debt)
WHERE NOT n.pg_id IN $alive
OPTIONAL MATCH (pr:Promise)-[:FOR]->(n)
WITH collect(DISTINCT n) + collect(DISTINCT pr) AS doomed
UNWIND doomed AS x
DETACH DELETE x
RETURN count(x)
"""


# ── reading memory ───────────────────────────────────────────────────────────
PARTY_NAMES = "MATCH (p:Party) RETURN p.key, p.name"

ALIAS_NAMES = """
MATCH (a:Alias)-[r:REFERS_TO]->(p:Party)
WHERE r.valid_to IS NULL
RETURN a.key, p.key, p.name
"""

# FalkorDB quirk: after a WITH … collect(), an OPTIONAL MATCH with any property filter
# (inline {open: true} or WHERE) matches nothing, while the same match unfiltered works.
# So nothing here is filtered in Cypher after the first aggregation: values are collected
# and filtered in Python (open debts, and valid_to for "still in force").
RECALL = """
MATCH (p:Party) WHERE p.key IN $keys
OPTIONAL MATCH (p)-[role]->(:Trader)
WITH p, collect([type(role), role.valid_to]) AS roles
OPTIONAL MATCH (a:Alias)-[ar:REFERS_TO]->(p)
WITH p, roles, collect([a.text, ar.valid_to]) AS aliases
OPTIONAL MATCH (d:Debt)-[:WITH]->(p)
WITH p, roles, aliases, collect([d.pg_id, d.open]) AS debts
OPTIONAL MATCH (pr:Promise)-[:MADE_BY]->(p)
WITH p, roles, aliases, debts, collect([pr.due, pr.status]) AS promises
OPTIONAL MATCH (f:Fact)-[:ABOUT]->(p)
RETURN p.key, p.name, roles, aliases, debts, promises,
       collect([f.kind, f.summary, f.said, f.valid_from, f.confidence, f.until, f.valid_to])
"""

CURRENT_FACT = """
MATCH (f:Fact {kind: $kind})-[:ABOUT]->(:Party {key: $key})
WHERE f.valid_to IS NULL
RETURN f.summary, f.said, f.data, f.confidence, f.until, f.valid_from
LIMIT 1
"""

DEBT_PARTY = "MATCH (d:Debt {pg_id: $pg_id})-[:WITH]->(p:Party) RETURN p.key, d.kind"

OPEN_PROMISES = """
MATCH (pr:Promise {status: 'open'})-[:FOR]->(:Debt {pg_id: $pg_id})
RETURN pr.id, pr.due
"""

# ── writing memory ───────────────────────────────────────────────────────────
# Provenance: every memory node links to the message it was learned from (if any) and the
# agent that wrote it. `x` is the node just created.
_SOURCE = """
WITH x
MERGE (ag:Agent {name: $agent})
MERGE (x)-[:WRITTEN_BY]->(ag)
WITH x
FOREACH (_ IN CASE WHEN $turn_id IS NULL THEN [] ELSE [1] END |
  MERGE (m:Message {turn_id: $turn_id}) ON CREATE SET m.at = $at
  MERGE (x)-[:LEARNED_FROM]->(m))
"""

ADD_ALIAS = """
MATCH (p:Party {key: $party_key})
MERGE (x:Alias {key: $alias_key})
ON CREATE SET x.text = $alias_text, x.kind = $kind
CREATE (x)-[:REFERS_TO {valid_from: $at}]->(p)
""" + _SOURCE

# a party with nothing attached: no debts, promises, facts, aliases or stated roles
REMOVE_EMPTY_PARTY = """
MATCH (p:Party {key: $key})
WHERE NOT (p)<-[:WITH]-(:Debt) AND NOT (p)<-[:MADE_BY]-(:Promise)
  AND NOT (p)<-[:ABOUT]-(:Fact) AND NOT (p)<-[:REFERS_TO]-(:Alias)
  AND NOT (p)-[:CUSTOMER_OF {confidence: 'stated'}]->(:Trader)
  AND NOT (p)-[:SUPPLIER_OF {confidence: 'stated'}]->(:Trader)
DETACH DELETE p
RETURN count(p)
"""

ADD_PROMISE = """
MATCH (d:Debt {pg_id: $pg_id})-[:WITH]->(p:Party)
CREATE (x:Promise {id: $id, due: $due, status: 'open', made_on: $made_on})
CREATE (x)-[:FOR]->(d), (x)-[:MADE_BY]->(p)
""" + _SOURCE

SET_PROMISE_STATUS = """
MATCH (pr:Promise {id: $id})
SET pr.status = $status, pr.paid_fraction = $paid_fraction, pr.closed_on = $on, pr.closed_by = $by
"""

OVERDUE_PROMISES = """
MATCH (pr:Promise {status: 'open'})-[:FOR]->(d:Debt)
WHERE pr.due < $today
RETURN pr.id, d.pg_id, pr.due, d.open, d.settled_on
"""

# ── reminders (Collector) ────────────────────────────────────────────────────
RECORD_REMINDER = """
MATCH (p:Party {key: $party_key})
MERGE (x:Reminder {pg_id: $pg_id})
ON CREATE SET x.sent_at = $at
MERGE (x)-[:SENT_TO]->(p)
""" + _SOURCE

# why it was sent: the promises still open or already broken when it went
LINK_REMINDER_REASONS = """
MATCH (x:Reminder {pg_id: $pg_id})-[:SENT_TO]->(p:Party)
MATCH (pr:Promise)-[:MADE_BY]->(p)
WHERE pr.status IN ['open', 'broken']
MERGE (x)-[:BECAUSE]->(pr)
RETURN count(pr)
"""

# ── explaining (the "why?" tool) ─────────────────────────────────────────────
# Simple chains of OPTIONAL MATCH without aggregation in between (see RECALL's note).
EXPLAIN_PROMISES = """
MATCH (pr:Promise)-[:MADE_BY]->(:Party {key: $key})
OPTIONAL MATCH (pr)-[:LEARNED_FROM]->(m:Message)
OPTIONAL MATCH (pr)-[:WRITTEN_BY]->(a:Agent)
OPTIONAL MATCH (pr)-[:FOR]->(d:Debt)
RETURN pr.due, pr.status, pr.paid_fraction, pr.made_on, pr.closed_on, pr.closed_by, m.turn_id, a.name, d.pg_id
ORDER BY pr.due
"""

EXPLAIN_FACTS = """
MATCH (f:Fact)-[:ABOUT]->(:Party {key: $key})
OPTIONAL MATCH (f)-[:LEARNED_FROM]->(m:Message)
OPTIONAL MATCH (f)-[:WRITTEN_BY]->(a:Agent)
RETURN f.kind, f.summary, f.said, f.confidence, f.valid_from, f.valid_to, f.until, f.data, m.turn_id, a.name
ORDER BY f.valid_from
"""

EXPLAIN_ALIASES = """
MATCH (al:Alias)-[r:REFERS_TO]->(:Party {key: $key})
OPTIONAL MATCH (al)-[:LEARNED_FROM]->(m:Message)
OPTIONAL MATCH (al)-[:WRITTEN_BY]->(a:Agent)
RETURN al.text, al.kind, r.valid_from, r.valid_to, m.turn_id, a.name
"""

EXPLAIN_ROLES = """
MATCH (:Party {key: $key})-[r]->(:Trader)
RETURN type(r), r.confidence, r.valid_from, r.valid_to, r.turn_id, r.written_by
"""

EXPLAIN_REMINDERS = """
MATCH (x:Reminder)-[:SENT_TO]->(:Party {key: $key})
OPTIONAL MATCH (x)-[:BECAUSE]->(pr:Promise)
RETURN x.pg_id, x.sent_at, collect([pr.due, pr.status])
ORDER BY x.sent_at
"""

CLOSE_FACT = """
MATCH (f:Fact {kind: $kind})-[:ABOUT]->(:Party {key: $key})
WHERE f.valid_to IS NULL
SET f.valid_to = $at, f.closed_by_turn = $turn_id
RETURN count(f)
"""

ADD_FACT = """
MATCH (p:Party {key: $key})
CREATE (x:Fact {id: $id, kind: $kind, summary: $summary, said: $said, data: $data,
                until: $until, confidence: $confidence, valid_from: $at})
CREATE (x)-[:ABOUT]->(p)
""" + _SOURCE

# Relationship types can't be query parameters, so each role has its own constants.
_ROLE_TYPES = {"customer": "CUSTOMER_OF", "supplier": "SUPPLIER_OF"}
DELETE_INFERRED_ROLE = {role: f"""
MATCH (:Party {{key: $key}})-[r:{rel} {{confidence: 'inferred'}}]->(:Trader)
DELETE r
""" for role, rel in _ROLE_TYPES.items()}
COUNT_CURRENT_STATED_ROLE = {role: f"""
MATCH (:Party {{key: $key}})-[r:{rel} {{confidence: 'stated'}}]->(:Trader)
WHERE r.valid_to IS NULL
RETURN count(r)
""" for role, rel in _ROLE_TYPES.items()}
CLOSE_STATED_ROLE = {role: f"""
MATCH (:Party {{key: $key}})-[r:{rel} {{confidence: 'stated'}}]->(:Trader)
WHERE r.valid_to IS NULL
SET r.valid_to = $at, r.closed_by_turn = $turn_id
RETURN count(r)
""" for role, rel in _ROLE_TYPES.items()}
# valid_to is set when the trader's statement *ends* a role nobody had stated before: the
# closed edge is still what stops the ledger from inferring it again (see sync)
CREATE_STATED_ROLE = {role: f"""
MATCH (p:Party {{key: $key}}), (t:Trader {{id: $user_id}})
CREATE (p)-[:{rel} {{confidence: 'stated', valid_from: $at, valid_to: $valid_to,
                    turn_id: $turn_id, written_by: $agent}}]->(t)
""" for role, rel in _ROLE_TYPES.items()}


def _iso(d) -> str | None:
    # dates are stored as ISO strings: they compare correctly as text and read clearly
    # in the FalkorDB Browser
    return d.isoformat() if d else None


class FalkorGraphMemory:
    enabled = True

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, password: str | None = None,
                 timeout_seconds: float = 2.0, graph_prefix: str = "trader_"):
        # short socket timeouts: graph work runs in the background, but a hung store must
        # not pile up tasks forever
        self._client_kwargs = dict(host=host, port=port, password=password or None,
                                   socket_timeout=timeout_seconds, socket_connect_timeout=timeout_seconds)
        self._db: FalkorDB | None = None  # built on first use, see _client()
        self._client_lock = asyncio.Lock()
        self._query_timeout_ms = int(timeout_seconds * 1000)
        self._prefix = graph_prefix
        self._ready: set[str] = set()  # graphs whose schema is in place
        self._schema_lock = asyncio.Lock()
        self._where = f"{host}:{port}"

    def graph_name(self, user_id: int) -> str:
        return f"{self._prefix}{int(user_id)}"

    # ── plumbing ────────────────────────────────────────────────────────────

    async def _client(self) -> FalkorDB:
        """The driver's constructor makes a blocking connection (it checks for cluster
        mode) and raises if FalkorDB is down. So it's built lazily, in a thread, and a
        failure is retried on the next call: the app starts, and reconnects, on its own."""
        if self._db is None:
            async with self._client_lock:
                if self._db is None:
                    try:
                        self._db = await asyncio.to_thread(FalkorDB, **self._client_kwargs)
                    except (RedisError, OSError) as e:
                        logger.warning("graph.connect_failed", where=self._where, error=type(e).__name__)
                        raise GraphMemoryError(f"can't reach FalkorDB at {self._where}: {type(e).__name__}") from e
                    logger.info("graph.connected", where=self._where)
        return self._db

    async def _run(self, graph, cypher: str, params: dict, op: str):
        started = time.perf_counter()
        try:
            result = await graph.query(cypher, params, timeout=self._query_timeout_ms)
        except (RedisError, OSError, asyncio.TimeoutError) as e:
            logger.warning("graph.query_failed", op=op, graph=graph.name, error=type(e).__name__,
                           detail=str(e)[:200])
            raise GraphMemoryError(f"graph {op} failed: {type(e).__name__}") from e
        logger.debug("graph.query", op=op, graph=graph.name, ms=round((time.perf_counter() - started) * 1000, 1))
        return result

    async def _ensure_schema(self, graph, user_id: int) -> None:
        for label, prop, unique in _INDEXES:
            try:
                await graph.create_node_range_index(label, prop)
            except ResponseError as e:
                if "already indexed" not in str(e).lower():
                    raise GraphMemoryError(f"creating index {label}.{prop} failed") from e
            if unique:
                try:
                    await graph.create_node_unique_constraint(label, prop)
                except ResponseError as e:
                    if "already exists" not in str(e).lower():
                        raise GraphMemoryError(f"creating constraint {label}.{prop} failed") from e
        await self._run(graph, MERGE_TRADER, {"user_id": int(user_id)}, "merge_trader")

    async def _graph(self, user_id: int):
        graph = (await self._client()).select_graph(self.graph_name(user_id))
        if graph.name in self._ready:
            return graph
        async with self._schema_lock:
            if graph.name not in self._ready:
                try:
                    await self._ensure_schema(graph, user_id)
                except (RedisError, OSError, asyncio.TimeoutError) as e:
                    logger.warning("graph.schema_failed", graph=graph.name, error=type(e).__name__)
                    raise GraphMemoryError(f"schema setup failed: {type(e).__name__}") from e
                self._ready.add(graph.name)
                logger.info("graph.schema_ready", graph=graph.name)
        return graph

    # ── port ────────────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        try:
            await (await self._client()).list_graphs()
            return True
        except Exception as e:
            logger.warning("graph.ping_failed", where=self._where, error=type(e).__name__)
            return False

    async def sync_parties(self, user_id: int, parties: list[PartySnapshot]) -> None:
        if not parties:
            return
        graph = await self._graph(user_id)
        now = datetime.now(timezone.utc).isoformat()

        for party in parties:
            if party.name is None:  # nothing left in the ledger: only clean up below
                await self._sync_inferred_role(graph, user_id, party)
                continue
            await self._run(graph, UPSERT_PARTY,
                            {"user_id": int(user_id), "key": party.key, "name": party.name, "now": now},
                            "upsert_party")
            if party.debts:
                await self._run(graph, UPSERT_DEBTS, {"key": party.key, "debts": [
                    {"pg_id": d.pg_id, "kind": d.kind, "open": d.open, "date": _iso(d.date), "due": _iso(d.due),
                     "settled_on": _iso(d.settled_on)}
                    for d in party.debts
                ]}, "upsert_debts")
            await self._sync_inferred_role(graph, user_id, party)

        keys = [p.key for p in parties]
        alive = [d.pg_id for p in parties for d in p.debts]
        result = await self._run(graph, REMOVE_STALE_DEBTS, {"keys": keys, "alive": alive}, "remove_stale_debts")
        removed = result.result_set[0][0] if result.result_set else 0
        kept = await self._run(graph, MARK_KEPT, {"keys": keys}, "mark_kept")
        kept_count = kept.result_set[0][0] if kept.result_set else 0
        logger.info("graph.synced", graph=graph.name, parties=len(parties), debts=len(alive), removed=removed,
                    promises_kept=kept_count)

    async def _sync_inferred_role(self, graph, user_id: int, party: PartySnapshot) -> None:
        # what the trader said beats what the ledger suggests: once a customer role has
        # been stated (or closed by a statement), the ledger no longer infers one
        stated = await self._run(graph, STATED_CUSTOMER_ROLES, {"key": party.key}, "stated_roles")
        if stated.result_set and stated.result_set[0][0] > 0:
            return
        if party.customer_since:
            await self._run(graph, SET_INFERRED_CUSTOMER,
                            {"user_id": int(user_id), "key": party.key, "since": _iso(party.customer_since)},
                            "set_inferred_customer")
        else:
            await self._run(graph, DROP_INFERRED_CUSTOMER, {"key": party.key}, "drop_inferred_customer")

    async def prune_debts(self, user_id: int, alive_pg_ids: list[int]) -> int:
        graph = await self._graph(user_id)
        result = await self._run(graph, PRUNE_DEBTS, {"alive": list(alive_pg_ids)}, "prune_debts")
        removed = result.result_set[0][0] if result.result_set else 0
        logger.info("graph.pruned", graph=graph.name, removed=removed)
        return removed

    async def delete_trader_graph(self, user_id: int) -> None:
        name = self.graph_name(user_id)
        db = await self._client()
        try:
            if name in await db.list_graphs():
                await db.select_graph(name).delete()
        except (RedisError, OSError, asyncio.TimeoutError) as e:
            logger.warning("graph.delete_failed", graph=name, error=type(e).__name__)
            raise GraphMemoryError(f"deleting {name} failed: {type(e).__name__}") from e
        self._ready.discard(name)
        logger.info("graph.deleted", graph=name)

    # ── reading memory ──────────────────────────────────────────────────────

    async def known_names(self, user_id: int) -> list[KnownName]:
        graph = await self._graph(user_id)
        parties = await self._run(graph, PARTY_NAMES, {}, "party_names")
        aliases = await self._run(graph, ALIAS_NAMES, {}, "alias_names")
        return ([KnownName(key=k, party_key=k, party_name=n, is_alias=False) for k, n in parties.result_set]
                + [KnownName(key=a, party_key=k, party_name=n, is_alias=True) for a, k, n in aliases.result_set])

    async def recall(self, user_id: int, party_keys: list[str]) -> list[PartyMemory]:
        if not party_keys:
            return []
        graph = await self._graph(user_id)
        result = await self._run(graph, RECALL, {"keys": list(party_keys)}, "recall")
        memories = []
        role_names = {"CUSTOMER_OF": "customer", "SUPPLIER_OF": "supplier"}
        for key, name, roles, aliases, debts, promises, facts in result.result_set:
            # collect() keeps [null, ...] when an OPTIONAL MATCH found nothing: filter those
            # out, along with anything closed (valid_to set)
            memories.append(PartyMemory(
                key=key, name=name,
                roles=tuple(sorted({role_names[t] for t, ended in roles if t in role_names and ended is None})),
                aliases=tuple(sorted({text for text, ended in aliases if text and ended is None})),
                open_debts=len({pg_id for pg_id, is_open in debts if pg_id is not None and is_open}),
                promises=tuple(sorted({PromiseView(due=d, status=st) for d, st in promises if d},
                                      key=lambda p: (p.due, p.status))),
                facts=tuple(FactView(kind=k, summary=sm, said=sd, since=since, confidence=c, until=u)
                            for k, sm, sd, since, c, u, ended in facts if k and ended is None),
            ))
        return memories

    async def current_fact(self, user_id: int, party_key: str, kind: str) -> dict | None:
        graph = await self._graph(user_id)
        result = await self._run(graph, CURRENT_FACT, {"key": party_key, "kind": kind}, "current_fact")
        if not result.result_set:
            return None
        summary, said, data, confidence, until, since = result.result_set[0]
        return {"summary": summary, "said": said, "data": data, "confidence": confidence,
                "until": until, "since": since}

    async def debt_party(self, user_id: int, pg_id: int) -> tuple[str, str] | None:
        graph = await self._graph(user_id)
        result = await self._run(graph, DEBT_PARTY, {"pg_id": int(pg_id)}, "debt_party")
        return tuple(result.result_set[0]) if result.result_set else None

    async def open_promises(self, user_id: int, pg_id: int) -> list[tuple[str, str]]:
        graph = await self._graph(user_id)
        result = await self._run(graph, OPEN_PROMISES, {"pg_id": int(pg_id)}, "open_promises")
        return [tuple(row) for row in result.result_set]

    # ── writing memory ──────────────────────────────────────────────────────

    @staticmethod
    def _source(source: Source) -> dict:
        return {"agent": source.agent, "at": source.at, "turn_id": source.turn_id}

    async def add_alias(self, user_id: int, alias_key: str, alias_text: str, party_key: str,
                        kind: str, source: Source) -> None:
        graph = await self._graph(user_id)
        await self._run(graph, ADD_ALIAS, {"alias_key": alias_key, "alias_text": alias_text, "party_key": party_key,
                                           "kind": kind, **self._source(source)}, "add_alias")

    async def remove_empty_party(self, user_id: int, party_key: str) -> bool:
        graph = await self._graph(user_id)
        result = await self._run(graph, REMOVE_EMPTY_PARTY, {"key": party_key}, "remove_empty_party")
        return bool(result.result_set and result.result_set[0][0])

    async def add_promise(self, user_id: int, pg_id: int, due: date, source: Source) -> str:
        graph = await self._graph(user_id)
        promise_id = str(uuid.uuid4())
        await self._run(graph, ADD_PROMISE, {"pg_id": int(pg_id), "id": promise_id, "due": _iso(due),
                                             "made_on": _iso(business_today()), **self._source(source)},
                        "add_promise")
        return promise_id

    async def set_promise_status(self, user_id: int, promise_id: str, status: str,
                                 paid_fraction: float | None, on: date, by: str | None = None) -> None:
        graph = await self._graph(user_id)
        await self._run(graph, SET_PROMISE_STATUS, {"id": promise_id, "status": status,
                                                    "paid_fraction": paid_fraction, "on": _iso(on), "by": by},
                        "set_promise_status")

    async def overdue_promises(self, user_id: int, today: date) -> list[OverduePromise]:
        graph = await self._graph(user_id)
        result = await self._run(graph, OVERDUE_PROMISES, {"today": _iso(today)}, "overdue_promises")
        return [OverduePromise(promise_id=i, pg_id=pg, due=due, debt_open=bool(is_open), settled_on=settled)
                for i, pg, due, is_open, settled in result.result_set]

    async def record_reminder(self, user_id: int, pg_id: int, party_key: str, source: Source) -> int:
        graph = await self._graph(user_id)
        await self._run(graph, RECORD_REMINDER, {"pg_id": int(pg_id), "party_key": party_key,
                                                 **self._source(source)}, "record_reminder")
        linked = await self._run(graph, LINK_REMINDER_REASONS, {"pg_id": int(pg_id)}, "link_reminder_reasons")
        return linked.result_set[0][0] if linked.result_set else 0

    async def explain(self, user_id: int, party_key: str) -> Explanation:
        graph = await self._graph(user_id)
        p = {"key": party_key}
        promises = await self._run(graph, EXPLAIN_PROMISES, p, "explain_promises")
        facts = await self._run(graph, EXPLAIN_FACTS, p, "explain_facts")
        aliases = await self._run(graph, EXPLAIN_ALIASES, p, "explain_aliases")
        roles = await self._run(graph, EXPLAIN_ROLES, p, "explain_roles")
        reminders = await self._run(graph, EXPLAIN_REMINDERS, p, "explain_reminders")
        role_names = {"CUSTOMER_OF": "customer", "SUPPLIER_OF": "supplier"}
        return Explanation(
            party_key=party_key,
            promises=[dict(due=due, status=st, paid_fraction=pf, made_on=made, closed_on=closed, closed_by=cb,
                           turn_id=t, by=a, entry_id=pg)
                      for due, st, pf, made, closed, cb, t, a, pg in promises.result_set],
            facts=[dict(kind=k, summary=sm, said=sd, confidence=c, since=vf, ended=vt, until=u, data=d, turn_id=t,
                        by=a)
                   for k, sm, sd, c, vf, vt, u, d, t, a in facts.result_set],
            aliases=[dict(text=tx, kind=k, since=vf, ended=vt, turn_id=t, by=a)
                     for tx, k, vf, vt, t, a in aliases.result_set],
            roles=[dict(role=role_names.get(r, r), confidence=c, since=vf, ended=vt, turn_id=t, by=w)
                   for r, c, vf, vt, t, w in roles.result_set],
            reminders=[dict(entry_id=pg, sent_at=at, because=[{"due": d, "status": st} for d, st in why if d])
                       for pg, at, why in reminders.result_set],
        )

    async def set_fact(self, user_id: int, party_key: str, kind: str, summary: str, source: Source,
                       confidence: str, said: str | None = None, data: str | None = None,
                       until: date | None = None) -> None:
        graph = await self._graph(user_id)
        await self._run(graph, CLOSE_FACT, {"key": party_key, "kind": kind, "at": source.at,
                                            "turn_id": source.turn_id}, "close_fact")
        await self._run(graph, ADD_FACT, {"key": party_key, "id": str(uuid.uuid4()), "kind": kind,
                                          "summary": summary, "said": said, "data": data, "until": _iso(until),
                                          "confidence": confidence, **self._source(source)}, "add_fact")

    async def close_fact(self, user_id: int, party_key: str, kind: str, source: Source) -> int:
        graph = await self._graph(user_id)
        result = await self._run(graph, CLOSE_FACT, {"key": party_key, "kind": kind, "at": source.at,
                                                     "turn_id": source.turn_id}, "close_fact")
        return result.result_set[0][0] if result.result_set else 0

    async def set_stated_role(self, user_id: int, party_key: str, role: str, active: bool,
                              source: Source) -> None:
        if role not in _ROLE_TYPES:
            raise GraphMemoryError(f"unknown role {role!r}")
        graph = await self._graph(user_id)
        params = {"key": party_key, "user_id": int(user_id), **self._source(source)}
        await self._run(graph, DELETE_INFERRED_ROLE[role], {"key": party_key}, "delete_inferred_role")
        if active:
            current = await self._run(graph, COUNT_CURRENT_STATED_ROLE[role], {"key": party_key}, "stated_role")
            if not (current.result_set and current.result_set[0][0]):
                await self._run(graph, CREATE_STATED_ROLE[role], {**params, "valid_to": None}, "open_stated_role")
            return
        closed = await self._run(graph, CLOSE_STATED_ROLE[role], params, "close_stated_role")
        if not (closed.result_set and closed.result_set[0][0]):
            await self._run(graph, CREATE_STATED_ROLE[role], {**params, "valid_to": source.at}, "closed_stated_role")

    async def close(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.aclose()
        except Exception as e:  # shutting down: nothing useful to do but note it
            logger.warning("graph.close_failed", error=type(e).__name__)

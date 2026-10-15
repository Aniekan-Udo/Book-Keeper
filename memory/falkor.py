"""FalkorDB adapter for the GraphMemory port.

One graph per trader (`trader_<user_id>`): a trader's agents can never read another
trader's memory, even through a buggy query, and queries need no trader filter.

Every Cypher statement the product runs lives in this file as a constant, with parameters
(never string-built), so the full list can be reviewed in one place.
"""
import asyncio
import time
from datetime import datetime, timezone

from falkordb.asyncio import FalkorDB
from redis.exceptions import RedisError, ResponseError

from memory.models import GraphMemoryError, PartySnapshot
from utils import logger

# ── schema ───────────────────────────────────────────────────────────────────
# (label, property, unique)
_INDEXES = (
    ("Party", "key", True),
    ("Debt", "pg_id", True),
    ("Alias", "key", False),
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
SET n.kind = d.kind, n.open = d.open, n.date = d.date, n.due = d.due
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

PRUNE_DEBTS = """
MATCH (n:Debt)
WHERE NOT n.pg_id IN $alive
OPTIONAL MATCH (pr:Promise)-[:FOR]->(n)
WITH collect(DISTINCT n) + collect(DISTINCT pr) AS doomed
UNWIND doomed AS x
DETACH DELETE x
RETURN count(x)
"""


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
                    {"pg_id": d.pg_id, "kind": d.kind, "open": d.open, "date": _iso(d.date), "due": _iso(d.due)}
                    for d in party.debts
                ]}, "upsert_debts")
            await self._sync_inferred_role(graph, user_id, party)

        alive = [d.pg_id for p in parties for d in p.debts]
        result = await self._run(graph, REMOVE_STALE_DEBTS,
                                 {"keys": [p.key for p in parties], "alive": alive}, "remove_stale_debts")
        removed = result.result_set[0][0] if result.result_set else 0
        logger.info("graph.synced", graph=graph.name, parties=len(parties), debts=len(alive), removed=removed)

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

    async def close(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.aclose()
        except Exception as e:  # shutting down: nothing useful to do but note it
            logger.warning("graph.close_failed", error=type(e).__name__)

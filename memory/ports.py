"""The port: what the app needs from a graph memory store, in its own terms.

Adapters implement it: memory/falkor.py (FalkorDB, the real one) and memory/null.py (does
nothing, for when graph memory is switched off). Application code depends only on this
Protocol, never on an adapter, so the store can be swapped or faked in tests.
"""
from datetime import date
from typing import Protocol

from memory.models import Explanation, KnownName, OverduePromise, PartyMemory, PartySnapshot, Source


class GraphMemory(Protocol):
    enabled: bool

    async def ping(self) -> bool:
        """True if the store answers. Never raises."""
        ...

    async def sync_parties(self, user_id: int, parties: list[PartySnapshot]) -> None:
        """Make the graph match the ledger for these parties: party nodes, their debts
        (moved to the right party if a correction changed it), and the inferred customer
        role. Debts of these parties that are no longer in the ledger are removed, along
        with any promise made about them. Never touches what the curator learned
        (aliases, facts, stated roles). Idempotent. Raises GraphMemoryError."""
        ...

    async def prune_debts(self, user_id: int, alive_pg_ids: list[int]) -> int:
        """Remove every debt (and its promises) not in `alive_pg_ids`. Used by a full
        rebuild to catch anything a missed sync left behind. Returns how many were removed.
        Raises GraphMemoryError."""
        ...

    async def delete_trader_graph(self, user_id: int) -> None:
        """Erase one trader's whole graph, learned memory included. Raises GraphMemoryError."""
        ...

    # ── reading memory ─────────────────────────────────────────────────────

    async def known_names(self, user_id: int) -> list[KnownName]:
        """Every party key and current alias, to find people mentioned in a message.
        Raises GraphMemoryError."""
        ...

    async def recall(self, user_id: int, party_keys: list[str]) -> list[PartyMemory]:
        """What's remembered about these parties. Raises GraphMemoryError."""
        ...

    async def current_fact(self, user_id: int, party_key: str, kind: str) -> dict | None:
        """The fact of this kind still in force for a party (its properties), if any."""
        ...

    async def debt_party(self, user_id: int, pg_id: int) -> tuple[str, str] | None:
        """(party key, debt kind) for a debt node, or None if the graph doesn't have it."""
        ...

    async def open_promises(self, user_id: int, pg_id: int) -> list[tuple[str, str]]:
        """(promise id, due) of open promises about one debt."""
        ...

    # ── writing memory (curator) ───────────────────────────────────────────
    # Every write records its Source: LEARNED_FROM the message, WRITTEN_BY the agent.

    async def add_alias(self, user_id: int, alias_key: str, alias_text: str, party_key: str,
                        kind: str, source: Source) -> None:
        ...

    async def close_alias(self, user_id: int, alias_key: str, party_key: str | None, source: Source) -> int:
        """Stop treating a name as this party (or as anyone, if party_key is None). The alias is
        closed (valid_to), not deleted: history stays true. Returns how many links were closed."""
        ...

    async def forget_party(self, user_id: int, party_key: str) -> dict:
        """Erase what's remembered about a party: aliases, facts, promises, reminders, stated
        roles. The party and its debts stay: they mirror the ledger, which is never touched.
        Returns counts of what was deleted."""
        ...

    async def remove_empty_party(self, user_id: int, party_key: str) -> bool:
        """Delete a party that has no debts, promises or facts (e.g. a misheard name that
        turned out to be an alias). Returns True if it was deleted."""
        ...

    async def add_promise(self, user_id: int, pg_id: int, due: date, source: Source) -> str:
        """A promise to pay this debt by `due`. Returns the promise id."""
        ...

    async def set_promise_status(self, user_id: int, promise_id: str, status: str,
                                 paid_fraction: float | None, on: date, by: str | None = None) -> None:
        """`by`: the agent (or "ledger_sync") that decided the outcome."""
        ...

    async def overdue_promises(self, user_id: int, today: date) -> list[OverduePromise]:
        """Open promises whose due date has passed, with the state of their debt."""
        ...

    async def record_reminder(self, user_id: int, pg_id: int, party_key: str, source: Source) -> int:
        """A reminder that was sent: SENT_TO the party, BECAUSE of their open and broken
        promises. Returns how many promises it was linked to."""
        ...

    async def explain(self, user_id: int, party_key: str) -> Explanation:
        """Everything remembered about a party, with where each piece came from."""
        ...

    async def set_fact(self, user_id: int, party_key: str, kind: str, summary: str, source: Source,
                       confidence: str, said: str | None = None, data: str | None = None,
                       until: date | None = None) -> None:
        """Record a fact, closing any earlier fact of the same kind for this party."""
        ...

    async def close_fact(self, user_id: int, party_key: str, kind: str, source: Source) -> int:
        """End the fact of this kind in force for a party. Returns how many were closed."""
        ...

    async def set_stated_role(self, user_id: int, party_key: str, role: str, active: bool,
                              source: Source) -> None:
        """What the trader said about a role ("na my supplier", "e no dey buy again"). A
        stated role replaces an inferred one and stops further inference."""
        ...

    async def close(self) -> None:
        ...

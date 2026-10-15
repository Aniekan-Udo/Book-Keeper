"""The port: what the app needs from a graph memory store, in its own terms.

Adapters implement it: memory/falkor.py (FalkorDB, the real one) and memory/null.py (does
nothing, for when graph memory is switched off). Application code depends only on this
Protocol, never on an adapter, so the store can be swapped or faked in tests.
"""
from typing import Protocol

from memory.models import PartySnapshot


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

    async def close(self) -> None:
        ...

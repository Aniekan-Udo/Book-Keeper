"""Null Object adapter: graph memory switched off (GRAPH_MEMORY=off, and in tests).
Every call succeeds and does nothing, so callers never need `if memory is not None`."""
from memory.models import PartySnapshot


class NullGraphMemory:
    enabled = False

    async def ping(self) -> bool:
        return False

    async def sync_parties(self, user_id: int, parties: list[PartySnapshot]) -> None:
        return None

    async def prune_debts(self, user_id: int, alive_pg_ids: list[int]) -> int:
        return 0

    async def delete_trader_graph(self, user_id: int) -> None:
        return None

    async def close(self) -> None:
        return None

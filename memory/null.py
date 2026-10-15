"""Null Object adapter: graph memory switched off (GRAPH_MEMORY=off, and in tests).
Every call succeeds and does nothing (reads return nothing), so callers never need
`if memory is not None`."""
from datetime import date

from memory.models import KnownName, PartyMemory, PartySnapshot, Source


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

    async def known_names(self, user_id: int) -> list[KnownName]:
        return []

    async def recall(self, user_id: int, party_keys: list[str]) -> list[PartyMemory]:
        return []

    async def current_fact(self, user_id: int, party_key: str, kind: str) -> dict | None:
        return None

    async def debt_party(self, user_id: int, pg_id: int) -> tuple[str, str] | None:
        return None

    async def open_promises(self, user_id: int, pg_id: int) -> list[tuple[str, str]]:
        return []

    async def add_alias(self, user_id: int, alias_key: str, alias_text: str, party_key: str,
                        kind: str, source: Source) -> None:
        return None

    async def remove_empty_party(self, user_id: int, party_key: str) -> bool:
        return False

    async def add_promise(self, user_id: int, pg_id: int, due: date, source: Source) -> str:
        return ""

    async def set_promise_status(self, user_id: int, promise_id: str, status: str,
                                 paid_fraction: float | None, on: date) -> None:
        return None

    async def set_fact(self, user_id: int, party_key: str, kind: str, summary: str, source: Source,
                       confidence: str, said: str | None = None, data: str | None = None,
                       until: date | None = None) -> None:
        return None

    async def close_fact(self, user_id: int, party_key: str, kind: str, source: Source) -> int:
        return 0

    async def set_stated_role(self, user_id: int, party_key: str, role: str, active: bool,
                              source: Source) -> None:
        return None

    async def close(self) -> None:
        return None

"""Plain data passed between the app and the graph memory port. No graph or DB types here,
so the port can be implemented (or faked in tests) without either."""
from dataclasses import dataclass, field
from datetime import date


class GraphMemoryError(RuntimeError):
    """The graph store failed (unreachable, timed out, rejected a query). Callers decide
    whether that matters: the ledger never does, memory features degrade."""


@dataclass(frozen=True)
class DebtSnapshot:
    """One ledger entry that is or was owed. Amounts stay in Postgres: the graph only
    needs to know the entry exists, which way it's owed, and whether it's still open."""
    pg_id: int
    kind: str  # "receivable" (they owe the trader) / "payable" (the trader owes them)
    open: bool
    date: date
    due: date | None = None


@dataclass(frozen=True)
class PartySnapshot:
    """Everything the ledger says about one party, as of now."""
    key: str   # normalised name, the same grouping the ledger uses for debts
    # how the trader last wrote it; None when the ledger no longer has any entry for them
    # (all undone), so the sync cleans up without creating a node for a typo
    name: str | None
    debts: tuple[DebtSnapshot, ...] = field(default_factory=tuple)
    # first sale / loan given to them, if any: the ledger's evidence they're a customer
    customer_since: date | None = None

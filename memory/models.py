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
    # date of the repayment that cleared it (closed debts only): decides whether a promise
    # was kept, using the ledger's dates rather than the day the sync happens to run
    settled_on: date | None = None


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


# ── learned memory ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    """Where a memory came from: the trader's message (a conversation_turns id) and the
    agent that wrote it. `turn_id` is None for what code inferred from the ledger."""
    agent: str
    at: str  # ISO timestamp
    turn_id: int | None = None


@dataclass(frozen=True)
class KnownName:
    """A name the trader uses for someone: a party's own key, or a current alias."""
    key: str
    party_key: str
    party_name: str
    is_alias: bool


@dataclass(frozen=True)
class FactView:
    kind: str           # usual_order / do_not_remind
    summary: str
    said: str | None
    since: str | None
    confidence: str     # stated / inferred
    until: str | None = None


@dataclass(frozen=True)
class PromiseView:
    due: str
    status: str         # open / kept / broken / replaced


@dataclass(frozen=True)
class PartyMemory:
    """What the graph knows about one party, for an agent's prompt."""
    key: str
    name: str
    roles: tuple[str, ...] = ()        # "customer", "supplier"
    aliases: tuple[str, ...] = ()
    open_debts: int = 0
    promises: tuple[PromiseView, ...] = ()
    facts: tuple[FactView, ...] = ()

    @property
    def kept(self) -> int:
        return sum(p.status == "kept" for p in self.promises)

    @property
    def broken(self) -> int:
        return sum(p.status == "broken" for p in self.promises)

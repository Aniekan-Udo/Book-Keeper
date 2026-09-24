"""Live check of how well the model turns real messages into ledger entries.

Runs each case through the real agent + tools against a throwaway in-memory database,
then compares what got recorded with what should have been. Uses real API calls, so it
is not part of the pytest suite — run it after changing the prompt or the model:

    uv run python -m tools.evals            # all cases
    uv run python -m tools.evals pidgin     # only cases whose name contains "pidgin"

Add a case whenever a real user message gets recorded wrong.
"""
import asyncio
import os
import sys
from dataclasses import dataclass, field

os.environ.setdefault("SECRET_KEY", "evals-only")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from db import Model, TransactionORM, User  # noqa: E402
from tools.assistant import respond  # noqa: E402

USER_ID = 1


@dataclass
class Case:
    name: str
    # earlier messages that set up the books (e.g. a credit sale before its repayment)
    setup: list[str]
    message: str
    # expected entries recorded by `message`; only the listed fields are compared
    expected: list[dict] = field(default_factory=list)
    # instead of checking new entries, check every entry in the books afterwards
    # (for messages that should update an existing entry rather than add one)
    expected_books: list[dict] | None = None


CASES = [
    Case("english cash sale", [], "I sold 20 bags of rice for 400k",
         [{"type": "sale", "amount": 400000, "outstanding": 0}]),
    Case("english buyer paid me", [], "James paid me 500k for cement",
         [{"type": "sale", "party": "James", "amount": 500000}]),
    Case("english i paid supplier", [], "I paid James 500k for cement",
         [{"type": "expense", "party": "James", "amount": 500000}]),
    Case("english part payment", [], "Sold 3 bags of cement to Emeka for 24k, he paid 10k",
         [{"type": "sale", "party": "Emeka", "amount": 24000, "outstanding": 14000}]),
    Case("english missing amount", [], "I sold 5 bags of beans",
         [{"type": "sale", "status": "needs_amount"}]),
    Case("english bundled loan", [], "I borrowed 50k from Sarah to buy fuel",
         [{"type": "loan_taken", "party": "Sarah", "amount": 50000},
          {"type": "expense", "amount": 50000}]),
    Case("pidgin credit sale", [], "Mama Chidi buy 3 crates of egg, 15k, she pay 10k, balance tomorrow",
         [{"type": "sale", "party": "Mama Chidi", "amount": 15000, "outstanding": 5000}]),
    Case("pidgin on credit", [], "I give Tunde 2 carton of indomie for 17k, he never pay",
         [{"type": "sale", "party": "Tunde", "amount": 17000, "outstanding": 17000}]),
    Case("pidgin repayment is not a sale",
         ["Sold 2 bags of cement to Emeka on credit, 16k"],
         "Emeka don pay the 16k wey he owe me",
         [{"type": "debt_repayment", "direction": "in", "party": "Emeka", "amount": 16000}]),
    Case("pidgin i paid my debt",
         ["Took goods from Alhaji worth 200k, paid 120k"],
         "I don pay Alhaji the 80k balance",
         [{"type": "debt_repayment", "direction": "out", "party": "Alhaji", "amount": 80000}]),
    Case("pidgin expense shorthand", [], "I spend 1.5m on shop rent",
         [{"type": "expense", "amount": 1500000}]),
    Case("pidgin loan given", [], "Bisi collect 30k loan from me",
         [{"type": "loan_given", "party": "Bisi", "amount": 30000}]),
    Case("memory amount follow-up", ["I sell 5 bags of beans to Tunde"], "15k",
         expected_books=[{"type": "sale", "party": "Tunde", "amount": 15000, "status": "complete"}]),
    Case("memory cancel that", ["Paid 20k for fuel"], "cancel that",
         expected_books=[{"type": "expense", "is_deleted": True}]),
    Case("memory correction", ["Paid 50k for fuel"], "sorry na 45k",
         expected_books=[{"type": "expense", "amount": 45000, "is_deleted": False}]),
]


def _matches(row: TransactionORM, expected: dict) -> bool:
    for key, want in expected.items():
        got = getattr(row, key)
        if isinstance(want, (int, float)):
            if got is None or float(got) != float(want):
                return False
        elif isinstance(want, str) and key == "party":
            if (got or "").strip().lower() != want.lower():
                return False
        elif got != want:
            return False
    return True


async def run_case(case: Case) -> tuple[bool, str]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    Session = async_sessionmaker(engine)
    try:
        async with Session() as db:
            db.add(User(id=USER_ID, firstname="Eval", lastname="User", email="eval@example.com", password="x"))
            await db.commit()
            # same path real users take, so conversation memory and pending amounts are in play
            for msg in case.setup:
                await respond(db, USER_ID, msg, channel="whatsapp")
            before = set((await db.execute(select(TransactionORM.id))).scalars().all())

            reply = (await respond(db, USER_ID, case.message, channel="whatsapp")).reply

            all_rows = (await db.execute(select(TransactionORM).execution_options(populate_existing=True))).scalars().all()
            if case.expected_books is not None:
                rows, expected = all_rows, case.expected_books
            else:
                rows, expected = [r for r in all_rows if r.id not in before], case.expected
            unmatched = list(rows)
            missing = []
            for exp in expected:
                hit = next((r for r in unmatched if _matches(r, exp)), None)
                if hit is None:
                    missing.append(exp)
                else:
                    unmatched.remove(hit)

            ok = not missing and not unmatched
            detail = "" if ok else (
                f"\n    missing:  {missing}"
                f"\n    recorded: {[{k: getattr(r, k) for k in ('type', 'direction', 'party', 'amount', 'outstanding', 'status', 'is_deleted')} for r in rows]}"
                f"\n    reply:    {reply!r}"
            )
            return ok, detail
    finally:
        await engine.dispose()


async def main(name_filter: str | None) -> int:
    cases = [c for c in CASES if not name_filter or name_filter in c.name]
    passed = 0
    for case in cases:
        try:
            ok, detail = await run_case(case)
        except Exception as e:  # keep going so one provider hiccup doesn't hide the rest
            ok, detail = False, f"\n    error: {e}"
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {case.name}{detail}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))

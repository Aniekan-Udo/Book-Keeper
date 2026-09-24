"""Ledger rules: settling debts, undo, corrections, and the reports built on them.

Runs against in-memory SQLite by default, so no Postgres needed:
    uv run pytest tools/test/test_ledger.py
Set TEST_DATABASE_URL to run against Postgres (tables are recreated per test).
"""
import os
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from config import GenerateDate
from db import Model, TransactionORM
from tools.formatting import describe_period
from tools.generate_PnL import generate_pnl
from tools.ledger import (
    LedgerError, TransactionInput, correct_transaction, delete_transactions,
    persist_transactions, resolve_clarification,
)
from tools.recievables import get_recievables, get_payables

USER = 1
OTHER_USER = 2
ALL_TIME = GenerateDate()


@pytest_asyncio.fixture
async def db():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url, poolclass=NullPool)
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.drop_all)
        await conn.run_sync(Model.metadata.create_all)
    # expire_on_commit left on, as in production, so lazy-load bugs surface here too
    Session = async_sessionmaker(engine)
    async with Session() as session:
        yield session
    await engine.dispose()


def sale(party=None, amount=None, amount_paid=None, day=None, item="rice"):
    return TransactionInput(type="sale", direction="in", party=party, item=item,
                            amount=amount, amount_paid=amount_paid, date=day)


def repayment(party, amount, direction="in"):
    return TransactionInput(type="debt_repayment", direction=direction, party=party, amount=amount)


async def record(db, *transactions, user_id=USER):
    return await persist_transactions(db, list(transactions), user_id)


async def reload(db, transaction_id) -> TransactionORM:
    return await db.get(TransactionORM, transaction_id, populate_existing=True)


# ── recording ────────────────────────────────────────────────────────────────

async def test_cash_sale_is_paid_in_full(db):
    result = await record(db, sale("Ada", 20000))
    logged = result["logged"][0]
    assert logged["amount_paid"] == 20000
    assert logged["outstanding"] == 0


async def test_credit_sale_is_a_receivable(db):
    await record(db, sale("Mama Chidi", 15000, amount_paid=10000))
    report = await get_recievables(db, ALL_TIME, USER)
    assert report["total_outstanding"] == 5000
    assert "Mama Chidi" in report["summary"]


async def test_money_is_stored_exactly(db):
    result = await record(db, sale("Ada", 0.1), sale("Ada", 0.2))
    rows = [await reload(db, r["id"]) for r in result["logged"]]
    assert sum(r.amount for r in rows) == Decimal("0.30")


# ── repayments ───────────────────────────────────────────────────────────────

async def test_repayment_settles_oldest_debts_first(db):
    first = (await record(db, sale("Emeka", 16000, amount_paid=0, day=date(2026, 9, 1))))["logged"][0]
    second = (await record(db, sale("Emeka", 10000, amount_paid=0, day=date(2026, 9, 5))))["logged"][0]

    result = await record(db, repayment("emeka ", 20000))

    assert (await reload(db, first["id"])).outstanding == 0
    assert (await reload(db, second["id"])).outstanding == Decimal("6000.00")
    assert any("still has ₦6,000.00 outstanding" in n for n in result["notes"])
    assert (await get_recievables(db, ALL_TIME, USER))["total_outstanding"] == 6000


async def test_repayment_does_not_count_as_new_revenue(db):
    await record(db, sale("Emeka", 16000, amount_paid=0))
    await record(db, repayment("Emeka", 16000))
    pnl = await generate_pnl(db, ALL_TIME, USER)
    assert pnl["revenue"] == 16000
    assert pnl["unpaid_sales"] == 0


async def test_repayment_without_matching_debt_is_flagged(db):
    result = await record(db, repayment("Stranger", 5000))
    assert "no open debt on record for Stranger" in result["notes"][0]


async def test_repayment_without_party_asks_who(db):
    result = await record(db, repayment(None, 5000))
    assert "Who was it?" in result["notes"][0]


async def test_repayment_only_touches_own_books(db):
    other = (await record(db, sale("Emeka", 16000, amount_paid=0), user_id=OTHER_USER))["logged"][0]
    await record(db, repayment("Emeka", 16000))
    assert (await reload(db, other["id"])).outstanding == Decimal("16000.00")


async def test_loans_both_ways(db):
    await record(db, TransactionInput(type="loan_given", direction="out", party="Tunde", amount=30000))
    await record(db, TransactionInput(type="loan_taken", direction="in", party="Bisi", amount=100000))
    await record(db, TransactionInput(type="expense", direction="out", party="Alhaji", item="rice",
                                      amount=200000, amount_paid=120000))

    assert (await get_recievables(db, ALL_TIME, USER))["total_outstanding"] == 30000
    assert (await get_payables(db, ALL_TIME, USER))["total_outstanding"] == 180000

    await record(db, repayment("Tunde", 30000, direction="in"))
    await record(db, repayment("Alhaji", 80000, direction="out"))

    assert (await get_recievables(db, ALL_TIME, USER))["total_outstanding"] == 0
    assert (await get_payables(db, ALL_TIME, USER))["total_outstanding"] == 100000


# ── reports ──────────────────────────────────────────────────────────────────

async def test_pnl_is_accrual_and_reports_unpaid(db):
    await record(db, sale("Ada", 50000, amount_paid=30000))
    await record(db, TransactionInput(type="expense", direction="out", item="fuel", amount=10000))
    pnl = await generate_pnl(db, ALL_TIME, USER)
    assert pnl["revenue"] == 50000
    assert pnl["net_profit"] == 40000
    assert pnl["unpaid_sales"] == 20000
    assert "₦20,000.00 of those sales is still owed to you" in pnl["summary"]


def test_describe_period_open_ranges():
    assert describe_period(date(2026, 3, 1), None) == "since March 01, 2026"
    assert describe_period(None, date(2026, 3, 1)) == "up to March 01, 2026"


# ── clarifications ───────────────────────────────────────────────────────────

async def test_clarified_amount_counts_as_revenue(db):
    result = await record(db, sale("Ada", None))
    assert result["clarifications"]
    txn_id = result["clarifications"][0]["transaction_id"]

    await resolve_clarification(db, USER, txn_id, 25000)

    assert (await generate_pnl(db, ALL_TIME, USER))["revenue"] == 25000
    assert (await reload(db, txn_id)).outstanding == 0


async def test_clarification_keeps_part_payment(db):
    result = await record(db, sale("Ada", None, amount_paid=5000))
    txn_id = result["clarifications"][0]["transaction_id"]
    await resolve_clarification(db, USER, txn_id, 25000)
    assert (await reload(db, txn_id)).outstanding == Decimal("20000.00")


async def test_clarified_repayment_settles_debt(db):
    debt = (await record(db, sale("Emeka", 16000, amount_paid=0)))["logged"][0]
    rep = (await record(db, repayment("Emeka", None)))["clarifications"][0]
    await resolve_clarification(db, USER, rep["transaction_id"], 16000)
    assert (await reload(db, debt["id"])).outstanding == 0


async def test_cannot_clarify_someone_elses_transaction(db):
    result = await record(db, sale("Ada", None), user_id=OTHER_USER)
    with pytest.raises(LedgerError):
        await resolve_clarification(db, USER, result["clarifications"][0]["transaction_id"], 25000)


# ── undo ─────────────────────────────────────────────────────────────────────

async def test_undo_removes_whole_last_message(db):
    await record(db, sale("Ada", 10000))
    await record(db, TransactionInput(type="loan_taken", direction="in", party="Bisi", amount=50000),
                 TransactionInput(type="expense", direction="out", item="fuel", amount=50000))

    result = await delete_transactions(db, USER)

    assert len(result["deleted"]) == 2
    pnl = await generate_pnl(db, ALL_TIME, USER)
    assert pnl["revenue"] == 10000 and pnl["expenses"] == 0
    assert (await get_payables(db, ALL_TIME, USER))["total_outstanding"] == 0


async def test_undo_repayment_reopens_debt(db):
    debt = (await record(db, sale("Emeka", 16000, amount_paid=0)))["logged"][0]
    await record(db, repayment("Emeka", 16000))
    await delete_transactions(db, USER)
    assert (await reload(db, debt["id"])).outstanding == Decimal("16000.00")


async def test_deleting_a_debt_moves_its_repayment_to_the_next_debt(db):
    wrong = (await record(db, sale("Emeka", 10000, amount_paid=0, day=date(2026, 9, 1))))["logged"][0]
    right = (await record(db, sale("Emeka", 10000, amount_paid=0, day=date(2026, 9, 2))))["logged"][0]
    await record(db, repayment("Emeka", 10000))  # settles `wrong`, the older one

    await delete_transactions(db, USER, wrong["id"])

    assert (await reload(db, right["id"])).outstanding == 0
    assert (await get_recievables(db, ALL_TIME, USER))["total_outstanding"] == 0


async def test_cannot_undo_someone_elses_transaction(db):
    other = (await record(db, sale("Ada", 10000), user_id=OTHER_USER))["logged"][0]
    with pytest.raises(LedgerError):
        await delete_transactions(db, USER, other["id"])


# ── corrections ──────────────────────────────────────────────────────────────

async def test_correcting_cash_sale_keeps_it_paid_in_full(db):
    txn = (await record(db, sale("Ada", 50000)))["logged"][0]
    result = await correct_transaction(db, USER, amount=45000)
    assert result["updated"]["id"] == txn["id"]
    row = await reload(db, txn["id"])
    assert row.amount_paid == Decimal("45000.00") and row.outstanding == 0


async def test_correcting_credit_sale_accounts_for_repayments(db):
    txn = (await record(db, sale("Emeka", 20000, amount_paid=5000)))["logged"][0]
    await record(db, repayment("Emeka", 5000))

    await correct_transaction(db, USER, txn["id"], amount=25000)

    row = await reload(db, txn["id"])
    assert row.amount_paid == Decimal("10000.00")
    assert row.outstanding == Decimal("15000.00")


async def test_correcting_party_moves_repayment(db):
    txn = (await record(db, sale("Tayo", 10000, amount_paid=0)))["logged"][0]
    tunde = (await record(db, sale("Tunde", 10000, amount_paid=0)))["logged"][0]
    await record(db, repayment("Tayo", 10000))

    await correct_transaction(db, USER, txn["id"], party="Tunde")

    # the repayment was from Tayo, who now has no debt, so both Tunde sales stay open
    assert (await reload(db, txn["id"])).outstanding == Decimal("10000.00")
    assert (await reload(db, tunde["id"])).outstanding == Decimal("10000.00")


async def test_correcting_ambiguous_last_message_asks_which(db):
    await record(db, sale("Ada", 10000), sale("Bola", 5000))
    with pytest.raises(LedgerError, match="Which one"):
        await correct_transaction(db, USER, amount=12000)

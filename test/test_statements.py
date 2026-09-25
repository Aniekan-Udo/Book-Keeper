"""
Customer statements: the running balance must always agree with the ledger, links must be
private/expiring/revocable, and the public page must be safe to show to outsiders.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main
from authentication.auth import hash_password
from authentication.settings import settings
from config import GenerateDate
from db import StatementLink, User, get_db_session
from tools import statements
from tools.clean_version import build_tools
from tools.ledger import TransactionInput, correct_transaction, delete_transactions, persist_transactions
from tools.recievables import get_payables, get_recievables
from tools.statements import StatementError, as_text, build_statement


@pytest_asyncio.fixture
async def trader(db_session):
    user = User(firstname="Ada", lastname="Obi", email="ada@example.com", password=hash_password("x" * 10),
                business_name="Ada Provisions")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "https://books.example.com")


async def record(db, user, *entries: dict):
    return await persist_transactions(db, [TransactionInput(**e) for e in entries], user.id)


def sale(party="Mama Chidi", amount=15000, paid=None, day=None, **kw):
    return {"type": "sale", "direction": "in", "party": party, "item": kw.pop("item", "egg"),
            "amount": amount, "amount_paid": paid, "date": day, **kw}


def paid_in(party="Mama Chidi", amount=2000, day=None):
    return {"type": "debt_repayment", "direction": "in", "party": party, "amount": amount, "date": day}


async def owed_by(db, user, party) -> Decimal:
    """What the ledger says: their open debts minus what the trader owes them."""
    receivable = sum(Decimal(str(r["outstanding"])) for r in (await get_recievables(db, GenerateDate(), user.id))["receivables"]
                     if r["party"].strip().lower() == party.lower())
    payable = sum(Decimal(str(r["outstanding"])) for r in (await get_payables(db, GenerateDate(), user.id))["payables"]
                  if r["party"].strip().lower() == party.lower())
    return receivable - payable


# ---------- Building ----------

async def test_statement_lines_and_running_balance(db_session, trader):
    await record(db_session, trader, sale(amount=15000, paid=10000, day=date(2026, 9, 22), quantity=3, unit="crates"))
    await record(db_session, trader, paid_in(amount=2000, day=date(2026, 9, 24)))

    st = await build_statement(db_session, trader, "mama chidi")

    assert [(l.description, l.amount, l.balance) for l in st.lines] == [
        ("Bought 3 crates egg", Decimal("15000.00"), Decimal("15000.00")),
        ("Payment", Decimal("-10000.00"), Decimal("5000.00")),
        ("Payment", Decimal("-2000.00"), Decimal("3000.00")),
    ]
    assert st.party == "Mama Chidi"
    assert st.balance_sentence() == "Mama Chidi owes Ada Provisions ₦3,000.00"


async def test_balance_always_matches_the_ledger(db_session, trader):
    """FIFO repayments across sales, an overpayment, a correction and a deletion."""
    await record(db_session, trader, sale(amount=10000, paid=0, day=date(2026, 9, 1)))
    await record(db_session, trader, sale(amount=8000, paid=3000, day=date(2026, 9, 3)))
    wrong = (await record(db_session, trader, sale(amount=4000, paid=0, day=date(2026, 9, 4))))["logged"][0]
    await record(db_session, trader, paid_in(amount=12000, day=date(2026, 9, 5)))
    await correct_transaction(db_session, trader.id, wrong["id"], amount=5000)
    await delete_transactions(db_session, trader.id, wrong["id"])
    await record(db_session, trader, paid_in(amount=1000, day=date(2026, 9, 6)))

    st = await build_statement(db_session, trader, "Mama Chidi")
    assert st.closing_balance == await owed_by(db_session, trader, "Mama Chidi") == Decimal("2000.00")


async def test_supplier_statement_runs_the_other_way(db_session, trader):
    await record(db_session, trader, {"type": "expense", "direction": "out", "party": "Alhaji", "item": "rice",
                                      "amount": 200000, "amount_paid": 120000, "date": date(2026, 9, 1)})
    st = await build_statement(db_session, trader, "Alhaji")
    assert st.closing_balance == Decimal("-80000.00") == await owed_by(db_session, trader, "Alhaji")
    assert st.balance_sentence() == "Ada Provisions owes Alhaji ₦80,000.00"

    await record(db_session, trader, {"type": "debt_repayment", "direction": "out", "party": "Alhaji", "amount": 80000})
    st = await build_statement(db_session, trader, "Alhaji")
    assert st.closing_balance == 0
    assert "all settled" in st.balance_sentence()


async def test_loans_appear_on_the_statement(db_session, trader):
    await record(db_session, trader, {"type": "loan_given", "direction": "out", "party": "Tunde", "amount": 30000})
    await record(db_session, trader, paid_in(party="Tunde", amount=10000))
    st = await build_statement(db_session, trader, "Tunde")
    assert [l.description for l in st.lines] == ["Loan", "Payment"]
    assert st.closing_balance == Decimal("20000.00") == await owed_by(db_session, trader, "Tunde")


async def test_period_brings_earlier_balance_forward(db_session, trader):
    await record(db_session, trader, sale(amount=10000, paid=0, day=date(2026, 8, 10)))
    await record(db_session, trader, sale(amount=5000, paid=0, day=date(2026, 9, 10)))
    st = await build_statement(db_session, trader, "Mama Chidi", start=date(2026, 9, 1))
    assert st.opening_balance == Decimal("10000.00")
    assert len(st.lines) == 1 and st.lines[0].balance == Decimal("15000.00")


async def test_deleted_and_pending_entries(db_session, trader):
    await record(db_session, trader, sale(amount=10000))
    await record(db_session, trader, sale(amount=7000))
    await delete_transactions(db_session, trader.id)
    await record(db_session, trader, sale(amount=None))
    st = await build_statement(db_session, trader, "Mama Chidi")
    assert len([l for l in st.lines if l.description.startswith("Bought")]) == 1
    assert st.pending_amounts == 1


async def test_only_this_traders_dealings(db_session, trader):
    other = User(firstname="B", lastname="C", email="b@example.com", password="x", business_name="B Stores")
    db_session.add(other)
    await db_session.commit()
    await record(db_session, other, sale(amount=99000))
    with pytest.raises(StatementError, match="don't have any dealings"):
        await build_statement(db_session, trader, "Mama Chidi")


async def test_text_version_keeps_recent_lines_and_carries_the_rest(db_session, trader):
    for day in range(1, 21):
        await record(db_session, trader, sale(amount=1000, paid=0, day=date(2026, 9, day)))
    text = as_text(await build_statement(db_session, trader, "Mama Chidi"), max_lines=5)
    lines = text.splitlines()
    assert lines[1] == "Brought forward: ₦15,000.00"
    assert len([l for l in lines if " · " in l]) == 5
    assert lines[-1] == "Balance: Mama Chidi owes Ada Provisions ₦20,000.00."


# ---------- Links ----------

async def test_share_link_is_reused_and_revocable(db_session, trader):
    await record(db_session, trader, sale())
    first = await statements.share_link(db_session, trader, "Mama Chidi")
    second = await statements.share_link(db_session, trader, "mama chidi")
    assert first == second and len(first) >= 32
    assert statements.link_url(first) == f"https://books.example.com/s/{first}"

    assert await statements.revoke_links(db_session, trader.id, "Mama Chidi") == 1
    assert await statements.resolve_link(db_session, first) is None
    third = await statements.share_link(db_session, trader, "Mama Chidi")
    assert third != first


async def test_expired_link_stops_working(db_session, trader):
    await record(db_session, trader, sale())
    token = await statements.share_link(db_session, trader, "Mama Chidi")
    link = await statements.resolve_link(db_session, token)
    link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    assert await statements.resolve_link(db_session, token) is None


async def test_no_link_without_a_public_url(db_session, trader, monkeypatch):
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "")
    await record(db_session, trader, sale())
    tools = {t.name: t for t in build_tools(db_session, trader.id)}
    result = await tools["customer_statement_tool"].ainvoke({"party": "Mama Chidi"})
    assert "link" not in result
    assert result["statement"].startswith("Statement: Mama Chidi — Ada Provisions")


async def test_statement_tool_returns_text_and_link(db_session, trader):
    await record(db_session, trader, sale(amount=15000, paid=10000))
    tools = {t.name: t for t in build_tools(db_session, trader.id)}
    result = await tools["customer_statement_tool"].ainvoke({"party": "Mama Chidi"})
    assert result["balance"] == "Mama Chidi owes Ada Provisions ₦5,000.00"
    assert result["link"].startswith("https://books.example.com/s/")


# ---------- Public page ----------

@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    main.app.dependency_overrides[get_db_session] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        yield ac
    main.app.dependency_overrides.clear()


async def test_public_page_shows_the_statement_privately(client, db_session, trader):
    await record(db_session, trader, sale(amount=15000, paid=10000))
    token = await statements.share_link(db_session, trader, "Mama Chidi")

    response = await client.get(f"/s/{token}")

    assert response.status_code == 200
    assert "Mama Chidi owes Ada Provisions ₦5,000.00" in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    # a record only: nothing to pay with
    assert "<form" not in response.text and "<button" not in response.text


async def test_public_page_escapes_what_the_trader_typed(client, db_session, trader):
    evil = '<script>alert(1)</script>'
    await record(db_session, trader, sale(party=evil, item='<img src=x onerror=alert(2)>'))
    token = await statements.share_link(db_session, trader, evil)
    response = await client.get(f"/s/{token}")
    assert "<script>alert(1)</script>" not in response.text
    assert "<img src=x" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.parametrize("token", ["not-a-real-token", "x" * 32])
async def test_unknown_link_is_a_plain_404(client, token):
    response = await client.get(f"/s/{token}")
    assert response.status_code == 404
    assert "isn't available" in response.text


async def test_revoked_link_is_a_404(client, db_session, trader):
    await record(db_session, trader, sale())
    token = await statements.share_link(db_session, trader, "Mama Chidi")
    await statements.revoke_links(db_session, trader.id, "Mama Chidi")
    assert (await client.get(f"/s/{token}")).status_code == 404

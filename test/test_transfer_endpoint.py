"""
Integration tests for /transfer.

ADAPT: `from main import app` assumes your FastAPI() instance is
importable as `app` from a `main.py` at the project root. Update this
import to match your actual entrypoint module.

These use httpx.AsyncClient against the app directly (no real network
call), with get_db / get_current_user / require_step_up overridden via
FastAPI's dependency_overrides so tests run against the isolated test
DB session and a known, pre-authenticated user — no real login/step-up
flow needed to exercise transfer logic itself.
"""
import asyncio
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from main import app
from db import get_db, User


@pytest_asyncio.fixture
async def client(db_session, test_user):
    async def override_get_db():
        yield db_session

    async def override_get_current_user():
        return test_user

    async def override_require_step_up():
        return str(test_user.id)

    # ADAPT: import paths for these three dependency functions —
    # update to wherever they actually live in your project.
    from db import get_db as get_db_dep
    from main import get_current_user as get_current_user_dep
    from authentication.auth import require_step_up as require_step_up_dep

    app.dependency_overrides[get_db_dep] = override_get_db
    app.dependency_overrides[get_current_user_dep] = override_get_current_user
    app.dependency_overrides[require_step_up_dep] = override_require_step_up

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_successful_transfer_moves_balance(client, test_user, test_recipient, db_session):
    idempotency_key = str(uuid.uuid4())

    response = await client.post("/transfer", json={
        "recipient_email": test_recipient.email,
        "amount": "100.00",
        "idempotency_key": idempotency_key,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Transfer successful"
    assert "reference" in body

    await db_session.refresh(test_user)
    await db_session.refresh(test_recipient)
    assert test_user.balance == 900   # started at 1000
    assert test_recipient.balance == 100  # started at 0


@pytest.mark.asyncio
async def test_transfer_insufficient_balance_returns_400(client, test_user, test_recipient):
    response = await client.post("/transfer", json={
        "recipient_email": test_recipient.email,
        "amount": "999999.00",  # far more than test_user's balance
        "idempotency_key": str(uuid.uuid4()),
    })
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_transfer_to_nonexistent_recipient_returns_404(client, test_user):
    response = await client.post("/transfer", json={
        "recipient_email": "nobody@example.com",
        "amount": "10.00",
        "idempotency_key": str(uuid.uuid4()),
    })
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_transfer_zero_or_negative_amount_rejected(client, test_recipient):
    for bad_amount in ["0.00", "-50.00"]:
        response = await client.post("/transfer", json={
            "recipient_email": test_recipient.email,
            "amount": bad_amount,
            "idempotency_key": str(uuid.uuid4()),
        })
        assert response.status_code == 400, f"amount={bad_amount} should be rejected"


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_does_not_double_charge(client, test_user, test_recipient, db_session):
    """
    The core idempotency guarantee: sending the same request twice
    (simulating a client retry) must only move money once.
    """
    idempotency_key = str(uuid.uuid4())
    payload = {
        "recipient_email": test_recipient.email,
        "amount": "50.00",
        "idempotency_key": idempotency_key,
    }

    first = await client.post("/transfer", json=payload)
    second = await client.post("/transfer", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    # Second response should be the replayed original result
    assert first.json()["reference"] == second.json()["reference"]

    await db_session.refresh(test_user)
    await db_session.refresh(test_recipient)
    # Balance should reflect exactly ONE transfer, not two
    assert test_user.balance == 950
    assert test_recipient.balance == 50


@pytest.mark.asyncio
async def test_concurrent_transfers_do_not_corrupt_balance(client, test_user, test_recipient, db_session):
    """
    Fires two concurrent transfer requests from the same sender and
    confirms the row lock prevents a lost-update race — the sender's
    final balance must reflect BOTH transfers, not just one
    overwriting the other's read.
    """
    amount = "100.00"

    async def do_transfer():
        return await client.post("/transfer", json={
            "recipient_email": test_recipient.email,
            "amount": amount,
            "idempotency_key": str(uuid.uuid4()),
        })

    results = await asyncio.gather(do_transfer(), do_transfer())

    assert all(r.status_code == 200 for r in results)

    await db_session.refresh(test_user)
    await db_session.refresh(test_recipient)
    # Sender started at 1000, two transfers of 100 each = 800 remaining
    assert test_user.balance == 800
    assert test_recipient.balance == 200
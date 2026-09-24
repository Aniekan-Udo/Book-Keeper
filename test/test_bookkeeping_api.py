"""
HTTP-level tests for registration and the authenticated bookkeeping endpoints.
The LLM is stubbed: these check who can touch which books, not the model.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from langchain_core.messages import AIMessage

import main
from db import get_db_session

SIGNUP = {
    "firstname": "Ada", "lastname": "Obi", "email": "Ada@Example.com",
    "password": "a-strong-password", "business_name": "Ada Provisions", "phone": "0803 123 4567",
}


@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    main.app.dependency_overrides[get_db_session] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        yield ac
    main.app.dependency_overrides.clear()


@pytest.fixture
def fake_llm(monkeypatch):
    """Stand-in agent: records whatever transactions the test queues up, via the real tool."""
    queued: list[dict] = []

    async def handle_message(user_message, system_prompt, tools):
        extract = next(t for t in tools if t.name == "extract_transactions_tool")
        if queued:
            await extract.ainvoke({"transactions": queued})
        return {"messages": [AIMessage(content="Recorded.")]}

    monkeypatch.setattr(main, "handle_message", handle_message)
    return queued


async def signup(client, **overrides) -> dict:
    response = await client.post("/register", json={**SIGNUP, **overrides})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ---------- Registration ----------

async def test_register_logs_in_and_normalizes_details(client):
    headers = await signup(client)
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["email"] == "ada@example.com"
    assert me["phone"] == "+2348031234567"
    assert me["business_name"] == "Ada Provisions"
    assert "refresh_token" in client.cookies


async def test_register_duplicate_email_is_rejected_case_insensitively(client):
    await signup(client)
    response = await client.post("/register", json={**SIGNUP, "email": "ADA@example.com", "phone": None})
    assert response.status_code == 409


async def test_register_duplicate_phone_is_rejected(client):
    await signup(client)
    response = await client.post("/register", json={**SIGNUP, "email": "new@example.com", "phone": "+234 803 123 4567"})
    assert response.status_code == 409


async def test_register_rejects_invalid_phone(client):
    response = await client.post("/register", json={**SIGNUP, "phone": "12345"})
    assert response.status_code == 422


async def test_login_is_case_insensitive_on_email(client):
    await signup(client)
    response = await client.post("/login", json={"email": "ADA@EXAMPLE.COM", "password": SIGNUP["password"]})
    assert response.status_code == 200


# ---------- Bookkeeping requires a login ----------

@pytest.mark.parametrize("path, body", [
    ("/api/v1/message", {"message": "I sold rice"}),
    ("/api/v1/clarify", {"transaction_id": 1, "amount": 5000}),
])
async def test_bookkeeping_endpoints_reject_anonymous_requests(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 401


async def test_bookkeeping_endpoints_reject_refresh_token_as_access_token(client):
    await signup(client)
    refresh_token = client.cookies["refresh_token"]
    response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {refresh_token}"})
    assert response.status_code == 401


async def test_user_id_in_body_is_ignored(client, fake_llm):
    ada = await signup(client)
    await signup(client, email="bola@example.com", phone=None)

    fake_llm.append({"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"})
    response = await client.post("/api/v1/message", json={"message": "I sold rice", "user_id": 2}, headers=ada)
    assert response.status_code == 200
    txn_id = response.json()["clarifications"][0]["transaction_id"]

    # the entry belongs to Ada (the token), so Ada can fill in the amount...
    ok = await client.post("/api/v1/clarify", json={"transaction_id": txn_id, "amount": 5000}, headers=ada)
    assert ok.status_code == 200
    assert "₦5,000.00" in ok.json()["reply"]


async def test_cannot_clarify_another_users_transaction(client, fake_llm):
    ada = await signup(client)
    bola = await signup(client, email="bola@example.com", phone=None)

    fake_llm.append({"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"})
    response = await client.post("/api/v1/message", json={"message": "I sold rice"}, headers=ada)
    txn_id = response.json()["clarifications"][0]["transaction_id"]

    stolen = await client.post("/api/v1/clarify", json={"transaction_id": txn_id, "amount": 1}, headers=bola)
    assert stolen.status_code == 404


async def test_idempotent_retry_does_not_record_twice(client, fake_llm, db_session):
    ada = await signup(client)
    fake_llm.append({"type": "expense", "direction": "out", "item": "fuel", "amount": 20000})
    headers = {**ada, "Idempotency-Key": "retry-123"}

    first = await client.post("/api/v1/message", json={"message": "paid 20k fuel"}, headers=headers)
    second = await client.post("/api/v1/message", json={"message": "paid 20k fuel"}, headers=headers)

    assert first.json() == second.json()
    from sqlalchemy import select, func
    from db import TransactionORM
    count = (await db_session.execute(select(func.count()).select_from(TransactionORM))).scalar()
    assert count == 1


# ---------- Logout ----------

async def test_logout_expires_cookie_on_the_path_it_was_set(client):
    await signup(client)
    response = await client.post("/logout")
    set_cookie = response.headers["set-cookie"]
    assert "refresh_token=" in set_cookie and "Path=/;" in set_cookie
    assert "refresh_token" not in client.cookies

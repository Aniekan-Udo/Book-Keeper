"""
HTTP-level tests for registration and the authenticated bookkeeping endpoints.
The LLM is stubbed: these check who can touch which books, not the model.
Each user gets their own browser (cookie jar) from the `new_browser` fixture.
"""
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from authentication.auth import SESSION_COOKIE
from db import TransactionORM
from tools import router

SIGNUP = {
    "firstname": "Ada", "lastname": "Obi", "email": "Ada@Example.com",
    "password": "a-strong-password", "business_name": "Ada Provisions",
}


@pytest.fixture
def fake_llm(monkeypatch):
    """Stand-in agent: records whatever transactions the test queues up, via the real tool."""
    queued: list[dict] = []

    async def handle_message(user_message, system_prompt, tools, history=None, callbacks=None):
        extract = next(t for t in tools if t.name == "extract_transactions_tool")
        if queued:
            await extract.ainvoke({"transactions": queued})
        return {"messages": [AIMessage(content="OK.")]}

    monkeypatch.setattr(router, "handle_message", handle_message)
    return queued


async def signed_up(new_browser, **overrides):
    """A browser that has just signed up (and so is logged in)."""
    browser = await new_browser()
    response = await browser.post("/register", json={**SIGNUP, **overrides})
    assert response.status_code == 201, response.text
    return browser


# ---------- Registration ----------

async def test_register_logs_in_and_normalizes_details(new_browser):
    ada = await signed_up(new_browser)
    me = (await ada.get("/api/v1/me")).json()
    assert me["email"] == "ada@example.com"
    assert me["phone"] is None  # connected later, by proving it from WhatsApp
    assert me["business_name"] == "Ada Provisions"
    assert SESSION_COOKIE in ada.cookies


async def test_register_duplicate_email_is_rejected_case_insensitively(new_browser):
    await signed_up(new_browser)
    other = await new_browser()
    response = await other.post("/register", json={**SIGNUP, "email": "ADA@example.com"})
    assert response.status_code == 409


async def test_web_signup_cannot_claim_a_phone_number(new_browser):
    """Security: a phone number decides whose books WhatsApp messages go into, so it must
    never come from a web form — only from proof sent from that WhatsApp number."""
    ada = await signed_up(new_browser, phone="0803 123 4567")
    assert (await ada.get("/api/v1/me")).json()["phone"] is None


async def test_login_is_case_insensitive_on_email(new_browser):
    await signed_up(new_browser)
    browser = await new_browser()
    response = await browser.post("/login", json={"email": "ADA@EXAMPLE.COM", "password": SIGNUP["password"]})
    assert response.status_code == 200


# ---------- Bookkeeping requires a login ----------

@pytest.mark.parametrize("path, body", [
    ("/api/v1/message", {"message": "I sold rice"}),
    ("/api/v1/clarify", {"transaction_id": 1, "amount": 5000}),
])
async def test_bookkeeping_endpoints_reject_anonymous_requests(new_browser, path, body):
    response = await (await new_browser()).post(path, json=body)
    assert response.status_code == 401


async def test_made_up_session_cookie_is_rejected(new_browser):
    browser = await new_browser()
    browser.cookies.set(SESSION_COOKIE, "not-a-real-session")
    assert (await browser.get("/api/v1/me")).status_code == 401


async def test_user_id_in_body_is_ignored(new_browser, fake_llm):
    ada = await signed_up(new_browser)
    await signed_up(new_browser, email="bola@example.com")

    fake_llm.append({"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"})
    response = await ada.post("/api/v1/message", json={"message": "I sold rice", "user_id": 2})
    assert response.status_code == 200
    txn_id = response.json()["clarifications"][0]["transaction_id"]

    # the entry belongs to Ada (the session), so Ada can fill in the amount
    ok = await ada.post("/api/v1/clarify", json={"transaction_id": txn_id, "amount": 5000})
    assert ok.status_code == 200
    assert "₦5,000.00" in ok.json()["reply"]


async def test_cannot_clarify_another_users_transaction(new_browser, fake_llm):
    ada = await signed_up(new_browser)
    bola = await signed_up(new_browser, email="bola@example.com")

    fake_llm.append({"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"})
    response = await ada.post("/api/v1/message", json={"message": "I sold rice"})
    txn_id = response.json()["clarifications"][0]["transaction_id"]

    stolen = await bola.post("/api/v1/clarify", json={"transaction_id": txn_id, "amount": 1})
    assert stolen.status_code == 404


async def test_idempotent_retry_does_not_record_twice(new_browser, fake_llm, db_session):
    ada = await signed_up(new_browser)
    fake_llm.append({"type": "expense", "direction": "out", "item": "fuel", "amount": 20000})
    headers = {"Idempotency-Key": "retry-123"}

    first = await ada.post("/api/v1/message", json={"message": "paid 20k fuel"}, headers=headers)
    second = await ada.post("/api/v1/message", json={"message": "paid 20k fuel"}, headers=headers)

    assert first.json() == second.json()
    count = (await db_session.execute(select(func.count()).select_from(TransactionORM))).scalar()
    assert count == 1


async def test_health_check(new_browser):
    response = await (await new_browser()).get("/health")
    # graph memory is the null adapter in tests (the lifespan never runs)
    assert response.status_code == 200 and response.json() == {"status": "ok", "graph": "off"}

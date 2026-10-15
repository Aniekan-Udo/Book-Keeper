"""
WhatsApp webhook + conversation memory. The LLM and Meta's send API are stubbed;
signatures, dedupe, user matching and history are real.
"""
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from sqlalchemy import select

import main
from authentication.auth import hash_password
from authentication.settings import settings
from channels import whatsapp
from db import ConversationTurn, TransactionORM, User, get_db_session
from tools import router
from tools.router import LLMUnavailable

APP_SECRET = "test-app-secret"
PHONE_NUMBER_ID = "1234567890"
ADA_WA = "2348031234567"


@pytest.fixture(autouse=True)
def whatsapp_configured(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", SecretStr("verify-me"))
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SecretStr(APP_SECRET))
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", SecretStr("token"))
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", PHONE_NUMBER_ID)
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "https://books.example.com")


@pytest.fixture
def sent(monkeypatch):
    outbox: list[tuple[str, str]] = []

    async def send_text(to, body):
        outbox.append((to, body))

    monkeypatch.setattr(whatsapp, "send_text", send_text)
    return outbox


@pytest.fixture
def agent(monkeypatch):
    """Stub agent. Each call is recorded; `agent.reply` / `agent.transactions` / `agent.error`
    control what the next call does."""
    class Agent:
        calls: list[dict] = []
        reply = "OK."  # not "Recorded.": the team re-runs an agent that claims a recording it never made
        transactions: list[dict] = []
        error: Exception | None = None

    async def handle_message(user_message, system_prompt, tools, history=None, callbacks=None):
        Agent.calls.append({"message": user_message, "prompt": system_prompt, "history": history})
        if Agent.error:
            raise Agent.error
        if Agent.transactions:
            extract = next(t for t in tools if t.name == "extract_transactions_tool")
            await extract.ainvoke({"transactions": Agent.transactions})
        return {"messages": [AIMessage(content=Agent.reply)]}

    Agent.calls = []
    monkeypatch.setattr(router, "handle_message", handle_message)
    return Agent


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    async def override_get_db():
        yield db_session

    @asynccontextmanager
    async def same_session():
        yield db_session

    main.app.dependency_overrides[get_db_session] = override_get_db
    monkeypatch.setattr(whatsapp, "session_factory", same_session)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
        yield ac
    main.app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def ada(db_session):
    user = User(firstname="Ada", lastname="Obi", email="ada@example.com", password=hash_password("x" * 10),
                business_name="Ada Provisions", phone="+" + ADA_WA)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def payload(*messages, phone_number_id=PHONE_NUMBER_ID, statuses=None) -> dict:
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "2349000000000", "phone_number_id": phone_number_id}}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}]}


def text_message(body, wamid="wamid.1", sender=ADA_WA) -> dict:
    return {"from": sender, "id": wamid, "timestamp": "1790000000", "type": "text", "text": {"body": body}}


async def post_signed(client, body: dict, secret=APP_SECRET):
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return await client.post("/webhooks/whatsapp", content=raw,
                             headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})


# ---------- Webhook verification ----------

async def test_verification_echoes_challenge(client):
    response = await client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert response.status_code == 200 and response.text == "12345"


async def test_verification_rejects_wrong_token(client):
    response = await client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"})
    assert response.status_code == 403


async def test_webhook_is_off_until_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", None)
    response = await post_signed(client, payload(text_message("hi")))
    assert response.status_code == 404


# ---------- Signatures ----------

async def test_rejects_unsigned_payload(client, sent):
    response = await client.post("/webhooks/whatsapp", json=payload(text_message("hi")))
    assert response.status_code == 403
    assert sent == []


async def test_rejects_payload_signed_with_wrong_secret(client, sent):
    response = await post_signed(client, payload(text_message("hi")), secret="attacker")
    assert response.status_code == 403
    assert sent == []


# ---------- Messages ----------

async def test_registered_user_gets_the_assistants_reply(client, ada, agent, sent):
    agent.reply = "Recorded: sold rice to Emeka for ₦45,000."
    response = await post_signed(client, payload(text_message("I sell rice to Emeka 45k")))

    assert response.status_code == 200
    assert sent == [(ADA_WA, "Recorded: sold rice to Emeka for ₦45,000.")]
    assert agent.calls[0]["message"] == "I sell rice to Emeka 45k"
    assert "WhatsApp: plain text" in agent.calls[0]["prompt"]


async def test_entries_are_recorded_for_the_sender(client, ada, agent, sent, db_session):
    agent.transactions = [{"type": "sale", "direction": "in", "party": "Emeka", "item": "rice", "amount": 45000}]
    await post_signed(client, payload(text_message("I sell rice to Emeka 45k")))

    recorded = (await db_session.execute(select(TransactionORM))).scalars().all()
    assert [(r.user_id, r.party) for r in recorded] == [(ada.id, "Emeka")]


async def test_unknown_number_starts_sign_up(client, agent, sent):
    await post_signed(client, payload(text_message("hello", sender="2348099999999")))
    assert len(sent) == 1
    to, body = sent[0]
    assert to == "2348099999999"
    assert "What's the name of your business?" in body
    assert agent.calls == []


async def test_redelivered_message_is_answered_once(client, ada, agent, sent):
    body = payload(text_message("I sell rice", wamid="wamid.same"))
    first = await post_signed(client, body)
    second = await post_signed(client, body)
    assert first.status_code == second.status_code == 200
    assert len(agent.calls) == 1
    assert len(sent) == 1


async def test_delivery_statuses_are_ignored(client, ada, agent, sent):
    response = await post_signed(client, payload(statuses=[{"id": "wamid.1", "status": "delivered"}]))
    assert response.status_code == 200
    assert sent == [] and agent.calls == []


async def test_messages_for_another_business_number_are_ignored(client, ada, agent, sent):
    await post_signed(client, payload(text_message("hi"), phone_number_id="999"))
    assert sent == [] and agent.calls == []


async def test_documents_get_a_polite_reply(client, ada, agent, sent):
    document = {"from": ADA_WA, "id": "wamid.doc", "timestamp": "1790000000", "type": "document",
                "document": {"id": "media-1", "mime_type": "application/pdf"}}
    await post_signed(client, payload(document))
    assert sent == [(ADA_WA, whatsapp.UNSUPPORTED_REPLY)]
    assert agent.calls == []


async def test_llm_outage_tells_the_user_to_retry(client, ada, agent, sent):
    agent.error = LLMUnavailable("down")
    await post_signed(client, payload(text_message("I sell rice")))
    assert sent == [(ADA_WA, whatsapp.BUSY_REPLY)]


# ---------- Conversation memory ----------

async def test_follow_up_sees_previous_exchange(client, ada, agent, sent):
    agent.reply = "How much was the rice?"
    await post_signed(client, payload(text_message("I sell rice to Emeka", wamid="wamid.a")))
    agent.reply = "Got it."
    await post_signed(client, payload(text_message("15k", wamid="wamid.b")))

    assert agent.calls[1]["history"] == [
        {"role": "user", "content": "I sell rice to Emeka"},
        {"role": "assistant", "content": "How much was the rice?"},
    ]


async def test_entries_waiting_for_an_amount_are_given_to_the_model(client, ada, agent, sent):
    agent.transactions = [{"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"}]
    await post_signed(client, payload(text_message("I sell rice to Emeka", wamid="wamid.a")))
    agent.transactions = []
    await post_signed(client, payload(text_message("15k", wamid="wamid.b")))

    prompt = agent.calls[1]["prompt"]
    assert "Entries waiting for an amount" in prompt
    assert "sale — rice (Emeka)" in prompt


async def test_old_conversation_is_forgotten(client, ada, agent, sent, db_session):
    db_session.add(ConversationTurn(user_id=ada.id, role="user", content="old message", channel="whatsapp",
                                    created_at=datetime.now(timezone.utc) - timedelta(hours=3)))
    await db_session.commit()
    await post_signed(client, payload(text_message("hi")))
    assert agent.calls[0]["history"] == []


async def test_web_and_whatsapp_share_memory(client, ada, agent, sent):
    await post_signed(client, payload(text_message("I sell rice to Emeka")))
    await client.post("/login", json={"email": "ada@example.com", "password": "x" * 10})  # session cookie
    await client.post("/api/v1/message", json={"message": "15k"})
    assert agent.calls[1]["history"][0]["content"] == "I sell rice to Emeka"


# ---------- Helpers ----------

def test_long_replies_are_split_under_the_limit():
    text = ("line of text\n" * 1000).strip()
    chunks = whatsapp._split(text, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == text


async def test_send_text_calls_the_cloud_api(monkeypatch):
    import httpx
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    await whatsapp.send_text(ADA_WA, "Hello ₦")

    [request] = requests
    assert str(request.url) == f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    assert request.headers["authorization"] == "Bearer token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": ADA_WA,
        "type": "text", "text": {"preview_url": False, "body": "Hello ₦"},
    }

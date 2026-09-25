"""
Every agent tool, called the way the agent calls it, on a session configured like
production (expire_on_commit=True). Catches reading ORM attributes after a commit — an
async lazy-load that crashes in production but passes on the lenient test session.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from pydantic import SecretStr

from authentication.settings import settings
from channels import whatsapp_api
from db import User
from tools.clean_version import build_tools


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "https://books.example.com")
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", SecretStr("v"))
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SecretStr("s"))
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", SecretStr("t"))
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "1")
    monkeypatch.setattr(settings, "WHATSAPP_REMINDER_TEMPLATE", "payment_reminder")

    async def send_template(to, name, language, params):
        return "wamid.x"
    monkeypatch.setattr(whatsapp_api, "send_template", send_template)


@pytest_asyncio.fixture
async def user_id(prod_session):
    user = User(firstname="Ada", lastname="Obi", email="ada@example.com", password="x", business_name="Ada Provisions")
    prod_session.add(user)
    await prod_session.flush()
    user_id = user.id  # read before commit expires it
    await prod_session.commit()
    return user_id


def tools_for(session, user_id, started=None):
    return {t.name: t for t in build_tools(session, user_id, [], request_started_at=started)}


async def test_every_tool_survives_commits(prod_session, user_id):
    tools = tools_for(prod_session, user_id)

    logged = await tools["extract_transactions_tool"].ainvoke({"transactions": [
        {"type": "sale", "direction": "in", "party": "Mama Chidi", "item": "egg", "amount": 15000,
         "amount_paid": 10000, "party_phone": "0803 123 4567"},
        {"type": "sale", "direction": "in", "party": "Emeka", "item": "rice"},
    ]})
    assert "error" not in logged and len(logged["logged"]) == 2

    for report in ("pnl", "expenses", "receivables", "payables"):
        assert "summary" in await tools["generate_report_tool"].ainvoke({"report_type": report})

    found = await tools["query_transactions_tool"].ainvoke({"query": {"item_or_party": "chidi"}})
    assert found["matches"] == 1

    emeka_id = logged["clarifications"][0]["transaction_id"]
    fixed = await tools["correct_transaction_tool"].ainvoke({"correction": {"transaction_id": emeka_id, "amount": 9000}})
    assert fixed["updated"]["amount"] == 9000

    saved = await tools["save_contact_tool"].ainvoke({"name": "Emeka", "phone": "08051112222"})
    assert saved == {"saved": "Emeka", "phone": "+2348051112222"}

    draft = await tools["remind_debtor_tool"].ainvoke({"party": "Mama Chidi"})
    assert "reminder_id" in draft, draft

    statement = await tools["customer_statement_tool"].ainvoke({"party": "Mama Chidi"})
    assert statement["link"].startswith("https://books.example.com/s/")
    assert (await tools["stop_sharing_statement_tool"].ainvoke({"party": "Mama Chidi"})) == {"links_withdrawn": 1}

    undone = await tools["undo_transaction_tool"].ainvoke({"transaction_id": emeka_id})
    assert len(undone["deleted"]) == 1

    # the trader confirms in their next message
    later = tools_for(prod_session, user_id, started=datetime.now(timezone.utc) + timedelta(seconds=1))
    sent = await later["confirm_reminder_tool"].ainvoke({"reminder_id": draft["reminder_id"]})
    assert sent.get("sent") is True, sent

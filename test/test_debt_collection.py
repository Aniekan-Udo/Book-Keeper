"""
Debt collection: phone numbers, due dates, and payment reminders with their guardrails
(confirmation in a later turn, opt-outs, cooldowns, daily cap), plus debtor replies.
Meta's send API is stubbed; the rules are real.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from authentication.auth import hash_password
from authentication.settings import settings
from channels import whatsapp, whatsapp_api
from config import GenerateDate
from db import ConversationTurn, Reminder, User
from tools import debt_collection
from tools.clean_version import build_tools
from tools.contacts import find_contact
from tools.debt_collection import ReminderRefused, confirm_reminder, draft_reminder
from tools.ledger import TransactionInput, correct_transaction, persist_transactions
from tools.recievables import get_recievables

# WhatsApp webhook fixtures and helpers
from test.test_whatsapp import (  # noqa: F401
    ADA_WA, ada, agent, client, payload, post_signed, sent, text_message, whatsapp_configured,
)

EMEKA_WA = "2348051112222"


@pytest.fixture(autouse=True)
def reminders_on(whatsapp_configured, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_REMINDER_TEMPLATE", "payment_reminder")
    monkeypatch.setattr(settings, "REMINDER_COOLDOWN_DAYS", 3)
    monkeypatch.setattr(settings, "REMINDER_DAILY_LIMIT", 20)


@pytest.fixture
def templates(monkeypatch):
    """Captured template sends. Set `templates.fail = True` to make Meta reject the next one."""
    class Templates:
        sent: list[dict] = []
        fail = False

    async def send_template(to, name, language, params):
        if Templates.fail:
            raise whatsapp_api.WhatsAppSendError("bad number")
        Templates.sent.append({"to": to, "name": name, "language": language, "params": params})
        return f"wamid.out{len(Templates.sent)}"

    Templates.sent = []
    monkeypatch.setattr(whatsapp_api, "send_template", send_template)
    return Templates


@pytest_asyncio.fixture
async def trader(db_session):
    user = User(firstname="Ada", lastname="Obi", email="trader@example.com", password=hash_password("x" * 10),
                business_name="Ada Provisions")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def credit_sale(db, user_id, party="Emeka", amount=16000, paid=0, phone=None, due=None, item="cement"):
    return await persist_transactions(db, [TransactionInput(
        type="sale", direction="in", party=party, item=item, amount=amount, amount_paid=paid,
        party_phone=phone, due_date=due)], user_id)


def just_now():
    return datetime.now(timezone.utc)


def next_request():
    """Start time of the trader's next message. Real messages are seconds apart; a small
    gap keeps this independent of clock resolution (~16 ms on Windows)."""
    return datetime.now(timezone.utc) + timedelta(seconds=1)


# ---------- Phone numbers and due dates ----------

async def test_phone_given_with_a_sale_is_saved(db_session, trader):
    result = await credit_sale(db_session, trader.id, phone="0805 111 2222")
    assert "Saved Emeka's number." in result["notes"]
    assert (await find_contact(db_session, trader.id, "emeka")).phone == "+" + EMEKA_WA


async def test_bad_phone_is_reported_but_sale_still_recorded(db_session, trader):
    result = await credit_sale(db_session, trader.id, phone="12345")
    assert len(result["logged"]) == 1
    assert any("doesn't look like a Nigerian phone number" in n for n in result["notes"])


async def test_overdue_debts_are_called_out(db_session, trader):
    await credit_sale(db_session, trader.id, party="Emeka", due=date(2020, 1, 10))
    await credit_sale(db_session, trader.id, party="Tunde", due=date(2999, 1, 1))
    summary = (await get_recievables(db_session, GenerateDate(), trader.id))["summary"]
    assert "Overdue: Emeka ₦16,000.00 (due Jan 10)" in summary
    assert "Tunde ₦16,000.00 (due" not in summary


async def test_due_date_can_be_added_later(db_session, trader):
    await credit_sale(db_session, trader.id)
    result = await correct_transaction(db_session, trader.id, due_date=date(2026, 9, 30))
    assert result["updated"]["due_date"] == "2026-09-30"


# ---------- Drafting ----------

async def test_draft_shows_exact_message_and_sends_nothing(db_session, trader, templates):
    await credit_sale(db_session, trader.id, amount=16000, phone="08051112222", item="cement")
    await credit_sale(db_session, trader.id, amount=10000, paid=4000, item="nails")

    draft = await draft_reminder(db_session, trader, "emeka")

    assert draft["to"] == "Emeka (0805 111 2222)"
    assert draft["message"].startswith("Hello Emeka, this is a friendly reminder from Ada Provisions.")
    assert "outstanding balance of ₦22,000.00" in draft["message"]
    assert "Reply STOP" in draft["message"]
    assert templates.sent == []


async def test_draft_needs_the_debtors_number(db_session, trader):
    await credit_sale(db_session, trader.id)
    with pytest.raises(ReminderRefused, match="don't have Emeka's phone number"):
        await draft_reminder(db_session, trader, "Emeka")


async def test_no_reminder_without_a_debt(db_session, trader):
    await credit_sale(db_session, trader.id, paid=16000, phone="08051112222")  # fully paid
    with pytest.raises(ReminderRefused, match="doesn't owe you anything"):
        await draft_reminder(db_session, trader, "Emeka")


async def test_reminders_off_without_approved_template(db_session, trader, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_REMINDER_TEMPLATE", None)
    await credit_sale(db_session, trader.id, phone="08051112222")
    with pytest.raises(ReminderRefused, match="aren't switched on"):
        await draft_reminder(db_session, trader, "Emeka")


# ---------- Confirming and sending ----------

async def test_confirmed_reminder_is_sent_as_the_approved_template(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222", item="cement")
    draft = await draft_reminder(db_session, trader, "Emeka")

    result = await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())

    assert result["sent"] is True
    [msg] = templates.sent
    assert msg["to"] == EMEKA_WA and msg["name"] == "payment_reminder" and msg["language"] == "en"
    assert msg["params"][:3] == ["Emeka", "Ada Provisions", "₦16,000.00"]
    assert msg["params"][3].startswith("cement (")
    reminder = await db_session.get(Reminder, draft["reminder_id"])
    assert reminder.status == "sent" and reminder.wa_message_id == "wamid.out1"


async def test_draft_cannot_be_sent_in_the_same_request(db_session, trader, templates):
    started = just_now()
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    with pytest.raises(ReminderRefused, match="hasn't seen this reminder"):
        await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=started)
    assert templates.sent == []


async def test_same_turn_guard_holds_through_the_agent_tools(db_session, trader, templates):
    """Even if the model calls draft and confirm back to back, nothing is sent."""
    await credit_sale(db_session, trader.id, phone="08051112222")
    tools = {t.name: t for t in build_tools(db_session, trader.id, request_started_at=just_now())}
    draft = await tools["remind_debtor_tool"].ainvoke({"party": "Emeka"})
    result = await tools["confirm_reminder_tool"].ainvoke({"reminder_id": draft["reminder_id"]})
    assert "error" in result
    assert templates.sent == []


async def test_expired_draft_is_not_sent(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    reminder = await db_session.get(Reminder, draft["reminder_id"])
    reminder.created_at = just_now() - timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(ReminderRefused, match="expired"):
        await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())
    assert templates.sent == []


async def test_cannot_send_another_traders_draft(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    other = User(firstname="B", lastname="C", email="b@example.com", password="x", business_name="B Stores")
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(ReminderRefused, match="couldn't find"):
        await confirm_reminder(db_session, other, draft["reminder_id"], not_before=next_request())


async def test_not_sent_if_debt_was_paid_after_draft(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    await persist_transactions(db_session, [TransactionInput(
        type="debt_repayment", direction="in", party="Emeka", amount=16000)], trader.id)
    with pytest.raises(ReminderRefused, match="no longer owes anything"):
        await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())
    assert templates.sent == []


async def test_not_sent_if_balance_changed_after_draft(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    await persist_transactions(db_session, [TransactionInput(
        type="debt_repayment", direction="in", party="Emeka", amount=6000)], trader.id)
    with pytest.raises(ReminderRefused, match="changed to ₦10,000.00"):
        await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())


async def test_failed_send_is_recorded_and_reported(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    templates.fail = True
    with pytest.raises(ReminderRefused, match="didn't accept"):
        await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())
    assert (await db_session.get(Reminder, draft["reminder_id"])).status == "failed"


# ---------- Limits ----------

async def test_one_reminder_per_debtor_per_cooldown(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())
    with pytest.raises(ReminderRefused, match="got a reminder recently"):
        await draft_reminder(db_session, trader, "Emeka")


async def test_daily_limit(db_session, trader, templates, monkeypatch):
    monkeypatch.setattr(settings, "REMINDER_DAILY_LIMIT", 1)
    await credit_sale(db_session, trader.id, party="Emeka", phone="08051112222")
    await credit_sale(db_session, trader.id, party="Tunde", phone="08053334444")
    draft = await draft_reminder(db_session, trader, "Emeka")
    await confirm_reminder(db_session, trader, draft["reminder_id"], not_before=next_request())
    with pytest.raises(ReminderRefused, match="daily limit"):
        await draft_reminder(db_session, trader, "Tunde")


async def test_opted_out_number_is_never_reminded(db_session, trader, templates):
    await credit_sale(db_session, trader.id, phone="08051112222")
    await debt_collection.opt_out(db_session, "+" + EMEKA_WA)
    with pytest.raises(ReminderRefused, match="asked not to receive reminders"):
        await draft_reminder(db_session, trader, "Emeka")


async def test_drafts_awaiting_confirmation_are_listed(db_session, trader):
    await credit_sale(db_session, trader.id, phone="08051112222")
    draft = await draft_reminder(db_session, trader, "Emeka")
    assert await debt_collection.pending_drafts(db_session, trader.id) == [
        f"#{draft['reminder_id']}: to Emeka for ₦16,000.00"]


# ---------- Replies from debtors (over the webhook) ----------

async def send_reminder_to_emeka(db, user) -> None:
    await credit_sale(db, user.id, phone="08051112222")
    draft = await draft_reminder(db, user, "Emeka")
    await confirm_reminder(db, user, draft["reminder_id"], not_before=next_request())


async def test_debtor_stop_opts_them_out(client, ada, sent, templates, db_session):
    await send_reminder_to_emeka(db_session, ada)
    await post_signed(client, payload(text_message("STOP", sender=EMEKA_WA)))
    assert sent == [(EMEKA_WA, whatsapp.OPTED_OUT_REPLY)]
    assert await debt_collection.is_opted_out(db_session, "+" + EMEKA_WA)


async def test_debtor_reply_is_acknowledged_and_passed_to_trader(client, ada, sent, templates, db_session):
    # the trader messaged us on WhatsApp recently, so we're allowed to message them
    db_session.add(ConversationTurn(user_id=ada.id, role="user", content="hi", channel="whatsapp"))
    await db_session.commit()
    await send_reminder_to_emeka(db_session, ada)

    await post_signed(client, payload(text_message("I go pay tomorrow", sender=EMEKA_WA)))

    to_debtor, to_trader = sent
    assert to_debtor[0] == EMEKA_WA and "Ada Provisions" in to_debtor[1] and "Reply STOP" in to_debtor[1]
    assert to_trader == (ADA_WA, "💬 Emeka replied to your reminder: “I go pay tomorrow”")


async def test_debtor_reply_not_forwarded_outside_the_24_hour_window(client, ada, sent, templates, db_session):
    await send_reminder_to_emeka(db_session, ada)
    await post_signed(client, payload(text_message("I go pay tomorrow", sender=EMEKA_WA)))
    assert [to for to, _ in sent] == [EMEKA_WA]


async def test_strangers_still_get_the_signup_link(client, sent):
    await post_signed(client, payload(text_message("hello", sender="2348077778888")))
    assert "signup=1" in sent[0][1]


async def test_trader_who_was_also_reminded_keeps_bookkeeping(client, ada, agent, sent, templates, db_session):
    other = User(firstname="Emeka", lastname="Eze", email="emeka@example.com", password="x",
                 business_name="Emeka Hardware", phone="+" + EMEKA_WA)
    db_session.add(other)
    await db_session.commit()
    await send_reminder_to_emeka(db_session, ada)

    await post_signed(client, payload(text_message("I sell 2 bags cement 16k", sender=EMEKA_WA)))
    assert agent.calls and agent.calls[0]["message"] == "I sell 2 bags cement 16k"


# ---------- The request Meta receives ----------

async def test_send_template_request_shape(monkeypatch):
    import httpx
    import json
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.tpl"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(whatsapp_api.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    wa_id = await whatsapp_api.send_template(EMEKA_WA, "payment_reminder", "en",
                                             ["Emeka", "Ada\nProvisions", "₦16,000.00", "cement   (Sep 03)"])

    assert wa_id == "wamid.tpl"
    body = json.loads(requests[0].content)
    assert body["type"] == "template" and body["to"] == EMEKA_WA
    assert body["template"]["name"] == "payment_reminder"
    assert body["template"]["language"] == {"code": "en"}
    # Meta rejects newlines / runs of spaces inside template parameters
    assert [p["text"] for p in body["template"]["components"][0]["parameters"]] == [
        "Emeka", "Ada Provisions", "₦16,000.00", "cement (Sep 03)"]


async def test_send_template_raises_when_meta_rejects(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    monkeypatch.setattr(whatsapp_api.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"code": 131026}})), **kw))
    with pytest.raises(whatsapp_api.WhatsAppSendError):
        await whatsapp_api.send_template(EMEKA_WA, "payment_reminder", "en", ["a", "b", "c", "d"])

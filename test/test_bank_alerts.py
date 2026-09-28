"""
Bank / POS alerts: what gets recorded automatically, what becomes a question, and the
protections (duplicates, fake "transfer successful" screens, secrets). The model's
field extraction is stubbed with ParsedAlert values; the decisions are real.
"""
from datetime import date

import pytest
import pytest_asyncio
from langchain_google_genai.chat_models import _convert_to_parts
from sqlalchemy import select

from authentication.auth import hash_password
from config import GenerateDate
from db import BankAlert, PartyAlias, TransactionORM, User
from tools import alerts
from tools.alerts import ParsedAlert, names_match, resolve_alert, review_alert
from tools.clean_version import build_tools
from tools.generate_PnL import generate_pnl
from tools.ledger import TransactionInput, delete_transactions, persist_transactions
from tools.recievables import get_payables, get_recievables

# WhatsApp webhook fixtures and helpers
from test.test_whatsapp import ADA_WA, ada, agent, client, payload, post_signed, sent, whatsapp_configured  # noqa: F401

DAY = date(2026, 9, 25)


@pytest_asyncio.fixture
async def trader(db_session):
    user = User(firstname="Ada", lastname="Obi", email="trader@example.com", password=hash_password("x" * 10),
                business_name="Ada Provisions")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def credit(amount, name="MAMA CHIDI OKAFOR", ref=None, kind="credit_alert", day=DAY, **kw) -> ParsedAlert:
    return ParsedAlert(is_alert=True, kind=kind, direction="in", amount=amount, counterparty=name,
                       narration=kw.pop("narration", f"TRF FROM {name}"), occurred_on=day, reference=ref, **kw)


def debit(amount, name, ref=None, day=DAY, narration=None) -> ParsedAlert:
    return ParsedAlert(is_alert=True, kind="debit_alert", direction="out", amount=amount, counterparty=name,
                       narration=narration or f"TRF TO {name}", occurred_on=day, reference=ref)


async def owes(db, user, party="Mama Chidi", amount=35000, day=date(2026, 9, 2)):
    await persist_transactions(db, [TransactionInput(type="sale", direction="in", party=party, item="egg",
                                                     amount=amount, amount_paid=0, date=day)], user.id)


async def receivable(db, user) -> float:
    return (await get_recievables(db, GenerateDate(), user.id))["total_outstanding"]


async def entries(db, user) -> list[TransactionORM]:
    return list((await db.execute(select(TransactionORM).where(
        TransactionORM.user_id == user.id, TransactionORM.is_deleted.is_(False)))).scalars().all())


# ---------- Name matching ----------

@pytest.mark.parametrize("party, bank_name, expected", [
    ("Mama Chidi", "MAMA CHIDI OKAFOR", True),
    ("Emeka", "EMEKA JOHN EZE", True),
    ("emeka eze", "NIP/EMEKA JOHN EZE/ref", True),
    ("Mama Chidi", "CHIDI OKAFOR", False),
    ("Tunde", "TUNDEX STORES", False),
    ("Tunde", None, False),
])
def test_names_match(party, bank_name, expected):
    assert names_match(party, bank_name) is expected


# ---------- Recorded automatically ----------

async def test_clear_repayment_is_recorded_straight_away(db_session, trader):
    await owes(db_session, trader, amount=35000)
    result = await review_alert(db_session, trader.id, "…", parsed=credit(10000, ref="100004260925"))

    assert result["status"] == "recorded"
    assert result["recorded_as"] == "Mama Chidi's payment"
    assert any("still has ₦25,000.00 outstanding" in n for n in result["notes"])
    assert await receivable(db_session, trader) == 25000
    alert = (await db_session.execute(select(BankAlert))).scalar_one()
    assert alert.status == "recorded" and alert.transaction_id is not None


async def test_payment_to_a_supplier_you_owe_is_recorded(db_session, trader):
    await persist_transactions(db_session, [TransactionInput(type="expense", direction="out", party="Alhaji Musa",
                                                             item="rice", amount=200000, amount_paid=120000)], trader.id)
    result = await review_alert(db_session, trader.id, "…", parsed=debit(80000, "ALHAJI MUSA SANI"))
    assert result["status"] == "recorded" and result["recorded_as"] == "your payment to Alhaji Musa"
    assert (await get_payables(db_session, GenerateDate(), trader.id))["total_outstanding"] == 0


# ---------- Questions instead of guesses ----------

async def test_unknown_sender_becomes_a_question(db_session, trader):
    result = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE"))
    assert result["status"] == "needs_answer"
    assert result["alert"] == "₦45,000.00 in from BOLA ADE on Sep 25"
    assert "new sale" in result["question"]
    assert await entries(db_session, trader) == []


async def test_two_possible_debtors_becomes_a_question(db_session, trader):
    await owes(db_session, trader, party="Emeka", amount=10000)
    await owes(db_session, trader, party="Emeka Eze", amount=20000)
    result = await review_alert(db_session, trader.id, "…", parsed=credit(5000, name="EMEKA JOHN EZE"))
    assert result["status"] == "needs_answer"
    assert "Emeka (owes ₦10,000.00)" in result["hints"][0] and "Emeka Eze (owes ₦20,000.00)" in result["hints"][0]


async def test_more_than_they_owe_becomes_a_question(db_session, trader):
    await owes(db_session, trader, amount=5000)
    result = await review_alert(db_session, trader.id, "…", parsed=credit(20000))
    assert result["status"] == "needs_answer"
    assert await receivable(db_session, trader) == 5000


async def test_money_out_without_a_match_asks_business_or_personal(db_session, trader):
    result = await review_alert(db_session, trader.id, "…",
                                parsed=debit(12500, "SHOPRITE IKEJA", narration="POS PURCHASE SHOPRITE IKEJA"))
    assert result["status"] == "needs_answer" and "personal" in result["question"]


# ---------- Protections ----------

async def test_senders_receipt_is_never_recorded_automatically(db_session, trader):
    await owes(db_session, trader, amount=35000)
    result = await review_alert(db_session, trader.id, "…", parsed=credit(10000, kind="sender_receipt"))
    assert result["status"] == "needs_answer"
    assert result["warning"] == "possible_fake_alert"
    assert "doesn't prove the money arrived" in result["question"]
    assert await receivable(db_session, trader) == 35000


async def test_alert_with_a_secret_is_not_stored(db_session, trader):
    result = await review_alert(db_session, trader.id, "…", parsed=credit(1000, contains_secret=True))
    assert result["status"] == "sensitive"
    assert (await db_session.execute(select(BankAlert))).first() is None


async def test_not_an_alert(db_session, trader):
    result = await review_alert(db_session, trader.id, "…", parsed=ParsedAlert(is_alert=False, kind="other"))
    assert result["status"] == "not_an_alert"


async def test_same_alert_twice_is_recorded_once(db_session, trader):
    await owes(db_session, trader, amount=35000)
    first = await review_alert(db_session, trader.id, "…", parsed=credit(10000, ref="REF1"))
    second = await review_alert(db_session, trader.id, "…", parsed=credit(10000, ref="REF1", narration="different text"))
    assert first["status"] == "recorded" and second["status"] == "duplicate_alert"
    assert await receivable(db_session, trader) == 25000


async def test_alert_can_be_used_again_after_its_entry_is_undone(db_session, trader):
    await owes(db_session, trader, amount=35000)
    await review_alert(db_session, trader.id, "…", parsed=credit(10000, ref="REF1"))
    await delete_transactions(db_session, trader.id)  # "cancel that"
    again = await review_alert(db_session, trader.id, "…", parsed=credit(10000, ref="REF1"))
    assert again["status"] == "recorded"
    assert await receivable(db_session, trader) == 25000


async def test_pending_alert_forwarded_again_asks_the_same_question(db_session, trader):
    first = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE", ref="R9"))
    second = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE", ref="R9"))
    assert second["status"] == "needs_answer" and second["alert_id"] == first["alert_id"]


async def test_money_already_typed_in_is_not_counted_twice(db_session, trader):
    typed = await persist_transactions(db_session, [TransactionInput(
        type="sale", direction="in", party="Emeka", item="rice", amount=45000, date=DAY)], trader.id)
    result = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="EMEKA JOHN EZE"))
    assert result["status"] == "needs_answer"
    assert result["possible_duplicate_of"] == typed["logged"][0]["id"]

    resolved = await resolve_alert(db_session, trader.id, result["alert_id"], "already_recorded",
                                   transaction_id=typed["logged"][0]["id"])
    assert resolved["status"] == "already_recorded"
    assert (await generate_pnl(db_session, GenerateDate(), trader.id))["revenue"] == 45000


# ---------- Resolving and learning names ----------

async def test_answer_records_it_and_learns_the_bank_name(db_session, trader):
    await owes(db_session, trader, amount=35000)
    # bank shows a name that doesn't obviously match "Mama Chidi"
    first = await review_alert(db_session, trader.id, "…", parsed=credit(10000, name="GRACE N OKAFOR", ref="A1"))
    assert first["status"] == "needs_answer"

    await resolve_alert(db_session, trader.id, first["alert_id"], "debt_repayment", party="Mama Chidi")
    assert await receivable(db_session, trader) == 25000
    alias = (await db_session.execute(select(PartyAlias))).scalar_one()
    assert (alias.alias_key, alias.party) == ("grace n okafor", "Mama Chidi")

    # next time, recognised automatically
    second = await review_alert(db_session, trader.id, "…", parsed=credit(5000, name="GRACE N OKAFOR", ref="A2"))
    assert second["status"] == "recorded" and second["recorded_as"] == "Mama Chidi's payment"


async def test_answer_as_new_sale_counts_as_revenue(db_session, trader):
    q = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE"))
    result = await resolve_alert(db_session, trader.id, q["alert_id"], "sale", party="Bola", item="2 bags rice")
    assert result["recorded"]["type"] == "sale" and result["recorded"]["date"] == "2026-09-25"
    assert (await generate_pnl(db_session, GenerateDate(), trader.id))["revenue"] == 45000


async def test_personal_money_is_ignored(db_session, trader):
    q = await review_alert(db_session, trader.id, "…", parsed=debit(12500, "SHOPRITE IKEJA"))
    assert (await resolve_alert(db_session, trader.id, q["alert_id"], "ignore"))["status"] == "ignored"
    assert await entries(db_session, trader) == []
    assert await alerts.pending_alerts(db_session, trader.id) == []


async def test_answer_must_fit_the_direction_of_the_money(db_session, trader):
    q = await review_alert(db_session, trader.id, "…", parsed=debit(12500, "SHOPRITE IKEJA"))
    with pytest.raises(alerts.AlertError, match="went out"):
        await resolve_alert(db_session, trader.id, q["alert_id"], "sale", item="rice")


async def test_cannot_resolve_another_users_alert(db_session, trader):
    q = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE"))
    other = User(firstname="B", lastname="C", email="b@example.com", password="x", business_name="B")
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(alerts.AlertError, match="couldn't find"):
        await resolve_alert(db_session, other.id, q["alert_id"], "ignore")


async def test_waiting_alerts_are_listed_for_the_assistant(db_session, trader):
    q = await review_alert(db_session, trader.id, "…", parsed=credit(45000, name="BOLA ADE"))
    assert await alerts.pending_alerts(db_session, trader.id) == [f"#{q['alert_id']}: ₦45,000.00 in from BOLA ADE on Sep 25"]


# ---------- Through the agent tools, on a production-like session ----------

async def test_alert_tools_on_production_session(prod_session, monkeypatch):
    user = User(firstname="Ada", lastname="Obi", email="p@example.com", password="x", business_name="Ada Provisions")
    prod_session.add(user)
    await prod_session.flush()
    user_id = user.id
    await prod_session.commit()
    await persist_transactions(prod_session, [TransactionInput(
        type="sale", direction="in", party="Mama Chidi", item="egg", amount=35000, amount_paid=0)], user_id)

    parsed = iter([credit(10000, ref="T1"), credit(45000, name="BOLA ADE", ref="T2")])

    async def fake_parse(text):
        return next(parsed)
    monkeypatch.setattr(alerts, "parse_alert_text", fake_parse)

    tools = {t.name: t for t in build_tools(prod_session, user_id)}
    recorded = await tools["review_bank_alert_tool"].ainvoke({"alert_text": "Credit Alert NGN10,000 ..."})
    assert recorded["status"] == "recorded"
    question = await tools["review_bank_alert_tool"].ainvoke({"alert_text": "Credit Alert NGN45,000 ..."})
    answer = await tools["resolve_bank_alert_tool"].ainvoke(
        {"alert_id": question["alert_id"], "record_as": "sale", "party": "Bola", "item": "rice"})
    assert answer["status"] == "recorded", answer


# ---------- Screenshots over WhatsApp ----------

def image_message(wamid="wamid.img", mime="image/jpeg", caption=None) -> dict:
    image = {"id": "media-1", "mime_type": mime}
    if caption:
        image["caption"] = caption
    return {"from": ADA_WA, "id": wamid, "timestamp": "1790000000", "type": "image", "image": image}


@pytest.fixture
def screenshot(monkeypatch):
    class Screenshot:
        text: str | None = "Credit Alert\nAmt: NGN10,000.00\nDesc: TRF FROM MAMA CHIDI OKAFOR"

    from channels import whatsapp

    async def download_media(media_id, max_bytes=None):
        return b"\x89PNG..."

    async def read_alert_image(data, mime_type):
        return Screenshot.text

    monkeypatch.setattr(whatsapp, "download_media", download_media)
    monkeypatch.setattr(alerts, "read_alert_image", read_alert_image)
    return Screenshot


async def test_alert_screenshot_reaches_the_assistant_as_text(client, ada, agent, sent, screenshot):
    await post_signed(client, payload(image_message(caption="from mama chidi")))
    assert agent.calls[0]["message"] == (
        "(forwarded alert screenshot) Credit Alert\nAmt: NGN10,000.00\nDesc: TRF FROM MAMA CHIDI OKAFOR"
        "\n(caption: from mama chidi)")


async def test_photo_that_isnt_an_alert(client, ada, agent, sent, screenshot):
    from channels import whatsapp
    screenshot.text = None
    await post_signed(client, payload(image_message()))
    assert sent == [(ADA_WA, whatsapp.IMAGE_NOT_ALERT_REPLY)]
    assert agent.calls == []


# ---------- What the model is sent ----------

async def test_image_is_sent_to_the_model_as_an_image(monkeypatch):
    captured = {}

    class FakeStructured:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return alerts.ImageReading(is_alert=True, text="Credit Alert NGN10,000")

    class FakeModel:
        def with_structured_output(self, schema):
            return FakeStructured()

    monkeypatch.setattr(alerts.llm, "get_vision_model", lambda: FakeModel())
    assert await alerts.read_alert_image(b"\x89PNG data", "image/png") == "Credit Alert NGN10,000"
    system, human = captured["messages"]
    assert "Never transcribe account balances" in system.content
    part = _convert_to_parts(human.content)[1]
    assert part.inline_data.mime_type == "image/png" and part.inline_data.data == b"\x89PNG data"


async def test_unsupported_image_type_is_not_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(alerts.llm, "get_vision_model", lambda: pytest.fail("model should not be called"))
    assert await alerts.read_alert_image(b"GIF89a", "image/gif") is None

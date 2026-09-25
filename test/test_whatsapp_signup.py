"""
Sign-up inside WhatsApp, and proving phone ownership by sending a code *from* WhatsApp
(passwordless web login, "Connect WhatsApp"). Meta and the LLM are stubbed.
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from authentication.settings import settings
from channels import onboarding
from db import OnboardingSession, PhoneChallenge, User
from tools import phone_auth

from test.test_whatsapp import agent, client, payload, post_signed, sent, text_message, whatsapp_configured  # noqa: F401

NEW = "2348090001111"
OTHER = "2348090002222"


@pytest.fixture(autouse=True)
def signup_settings(whatsapp_configured, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_DISPLAY_NUMBER", "2349000000000")
    monkeypatch.setattr(settings, "PRIVACY_POLICY_URL", "https://books.example.com/privacy")


_message_ids = itertools.count()


async def say(client, text, sender=NEW):
    """Send a WhatsApp text from `sender`, as Meta would deliver it."""
    await post_signed(client, payload(text_message(text, sender=sender, wamid=f"wamid.s{next(_message_ids)}")))


async def user_count(db) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar()


async def sign_up_on_whatsapp(client, sender=NEW, business="Ada Provisions", name="Ada Obi"):
    await say(client, "hi", sender)
    await say(client, business, sender)
    await say(client, name, sender)


# ---------- Sign-up conversation ----------

async def test_sign_up_in_two_answers(client, sent, agent, db_session):
    await say(client, "hi")
    assert "What's the name of your business?" in sent[-1][1]
    assert "https://books.example.com/privacy" in sent[-1][1]
    await say(client, "Ada Provisions")
    assert sent[-1][1] == onboarding.ASK_NAME
    await say(client, "Ada Obi")
    assert sent[-1][1].startswith("You're set up, Ada!")
    assert "Log in with WhatsApp" in sent[-1][1]

    user = (await db_session.execute(select(User))).scalar_one()
    assert (user.firstname, user.lastname, user.business_name, user.phone) == ("Ada", "Obi", "Ada Provisions", "+" + NEW)
    assert user.email is None and user.password is None

    # from now on, messages are bookkeeping
    await say(client, "I sell 2 bags rice 90k")
    assert agent.calls[-1]["message"] == "I sell 2 bags rice 90k"


async def test_no_account_until_both_questions_are_answered(client, sent, db_session):
    await say(client, "hi")
    await say(client, "Ada Provisions")
    assert await user_count(db_session) == 0


async def test_first_message_that_was_a_sale_is_recorded_after_sign_up(client, sent, agent):
    await say(client, "I sell 2 bags rice to Emeka 90k")
    await say(client, "Ada Provisions")
    await say(client, "Ada")
    assert [c["message"] for c in agent.calls] == ["I sell 2 bags rice to Emeka 90k"]


async def test_greeting_is_not_replayed(client, sent, agent):
    await sign_up_on_whatsapp(client)
    assert agent.calls == []


async def test_sign_up_can_be_cancelled(client, sent, db_session):
    await say(client, "hi")
    await say(client, "cancel")
    assert sent[-1][1] == onboarding.CANCELLED
    await say(client, "hello again")
    assert "What's the name of your business?" in sent[-1][1]


async def test_voice_note_during_sign_up_asks_to_type(client, sent):
    await say(client, "hi")
    await post_signed(client, payload({"from": NEW, "id": "wamid.v", "type": "audio",
                                       "audio": {"id": "m", "mime_type": "audio/ogg"}}))
    assert sent[-1][1] == onboarding.TYPE_PLEASE


async def test_abandoned_sign_up_starts_over(client, sent, db_session):
    await say(client, "hi")
    await say(client, "Ada Provisions")
    session = (await db_session.execute(select(OnboardingSession))).scalar_one()
    session.updated_at = datetime.now(timezone.utc) - timedelta(days=2)
    await db_session.commit()
    await say(client, "Ada")
    assert "What's the name of your business?" in sent[-1][1]
    assert await user_count(db_session) == 0


async def test_welcome_without_privacy_url(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_POLICY_URL", None)
    assert "Your records are private" in onboarding._welcome()


# ---------- Log in on the web with a WhatsApp code ----------

async def start_login(client, phone="0809 000 1111"):
    response = await client.post("/auth/whatsapp/login", json={"phone": phone})
    assert response.status_code == 200, response.text
    return response.json()


async def test_log_in_with_whatsapp(client, sent, db_session):
    await sign_up_on_whatsapp(client)
    challenge = await start_login(client)
    assert challenge["message"] == f"LOGIN {challenge['code']}"
    assert challenge["whatsapp_link"] == f"https://wa.me/2349000000000?text=LOGIN%20{challenge['code']}"
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json() == {"status": "pending"}

    await say(client, f"login {challenge['code']}")
    assert sent[-1][1] == phone_auth.LOGGED_IN_REPLY

    approved = (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json()
    assert approved["status"] == "approved"
    me = (await client.get("/api/v1/me", headers={"Authorization": f"Bearer {approved['access_token']}"})).json()
    assert me["business_name"] == "Ada Provisions"
    assert "refresh_token" in client.cookies

    # a login can only be collected once
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json() == {"status": "expired"}


async def test_code_sent_from_another_number_does_nothing(client, sent):
    await sign_up_on_whatsapp(client)
    await sign_up_on_whatsapp(client, sender=OTHER, business="Other Shop")
    challenge = await start_login(client)  # for NEW's number

    await say(client, f"LOGIN {challenge['code']}", sender=OTHER)

    assert sent[-1] == (OTHER, phone_auth.INVALID_CODE_REPLY)
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json() == {"status": "pending"}


async def test_login_for_number_without_account_is_refused_on_whatsapp_only(client, sent):
    # the website response looks the same whether or not the number has an account
    challenge = await start_login(client)
    await say(client, f"LOGIN {challenge['code']}")
    assert sent[-1][1] == phone_auth.NO_ACCOUNT_REPLY
    result = (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json()
    assert result["status"] == "rejected"


async def test_expired_code_is_refused(client, sent, db_session):
    await sign_up_on_whatsapp(client)
    challenge = await start_login(client)
    row = (await db_session.execute(select(PhoneChallenge))).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    await say(client, f"LOGIN {challenge['code']}")
    assert sent[-1][1] == phone_auth.INVALID_CODE_REPLY
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json() == {"status": "expired"}


async def test_invalid_phone_is_rejected(client):
    assert (await client.post("/auth/whatsapp/login", json={"phone": "12345678"})).status_code == 422


async def test_endpoints_are_off_without_whatsapp(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", None)
    assert (await client.post("/auth/whatsapp/login", json={"phone": "08090001111"})).status_code == 404


async def test_whatsapp_only_account_cannot_password_login(client, sent, db_session):
    await sign_up_on_whatsapp(client)
    user = (await db_session.execute(select(User))).scalar_one()
    user.email = "ada@example.com"  # even with an email, there's no password to check
    await db_session.commit()
    response = await client.post("/login", json={"email": "ada@example.com", "password": "anything-at-all"})
    assert response.status_code == 401


async def test_logout_on_whatsapp_ends_web_sessions(client, sent):
    await sign_up_on_whatsapp(client)
    challenge = await start_login(client)
    await say(client, f"LOGIN {challenge['code']}")
    await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")
    assert (await client.post("/refresh")).status_code == 200

    await say(client, "logout")

    assert sent[-1][1].startswith("Done — you've been signed out")
    assert (await client.post("/refresh")).status_code == 401


# ---------- Connect WhatsApp to a web account ----------

async def web_account(client, email="web@example.com"):
    response = await client.post("/register", json={"firstname": "Bola", "lastname": "Ade", "email": email,
                                                    "password": "a-strong-password", "business_name": "Bola Stores"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_connect_whatsapp_to_a_web_account(client, sent, agent):
    headers = await web_account(client)
    challenge = (await client.post("/auth/whatsapp/link", json={"phone": "08090001111"}, headers=headers)).json()
    assert challenge["message"].startswith("LINK ")

    await say(client, challenge["message"])
    assert "now connected to Bola Stores" in sent[-1][1]
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json() == {"status": "approved"}
    assert (await client.get("/api/v1/me", headers=headers)).json()["phone"] == "+" + NEW

    # and WhatsApp messages from that number now go into this account's books
    await say(client, "I sell rice 5k")
    assert agent.calls[-1]["message"] == "I sell rice 5k"


async def test_cannot_connect_a_number_that_belongs_to_someone_else(client, sent, db_session):
    await sign_up_on_whatsapp(client)  # NEW already has its own account
    headers = await web_account(client)
    challenge = (await client.post("/auth/whatsapp/link", json={"phone": "08090001111"}, headers=headers)).json()

    await say(client, challenge["message"])

    assert sent[-1][1] == phone_auth.NUMBER_TAKEN_REPLY
    assert (await client.post(f"/auth/whatsapp/challenges/{challenge['challenge_id']}")).json()["status"] == "rejected"
    assert (await client.get("/api/v1/me", headers=headers)).json()["phone"] is None


async def test_the_hijack_this_prevents(client, sent, agent, db_session):
    """An attacker who knows a trader's number can start a LINK for it, but only a message
    from that phone completes it — so the trader's WhatsApp never feeds the attacker's books."""
    attacker = await web_account(client, email="attacker@example.com")
    challenge = (await client.post("/auth/whatsapp/link", json={"phone": "08090001111"}, headers=attacker)).json()
    await say(client, challenge["message"], sender=OTHER)  # attacker sends it from their own phone
    assert (await client.get("/api/v1/me", headers=attacker)).json()["phone"] is None

    await say(client, "hi")  # the real owner of the number just gets sign-up
    assert "What's the name of your business?" in sent[-1][1]


async def test_connect_requires_login(client):
    assert (await client.post("/auth/whatsapp/link", json={"phone": "08090001111"})).status_code == 401


@pytest.mark.parametrize("text, expected", [
    ("LOGIN 123456", ("login", "123456")),
    ("  link 000001. ", ("link", "000001")),
    ("login 12345", None),
    ("I sold 123456 naira of rice", None),
])
def test_code_messages(text, expected):
    assert phone_auth.parse_code_message(text) == expected


async def test_burst_of_messages_is_handled_in_order(client, sent, db_session):
    """Answers typed quickly (or queued while the phone was offline) arrive together; they
    must still go through sign-up one after another, in order."""
    import asyncio
    from channels import whatsapp
    burst = [text_message(t, sender=NEW, wamid=f"wamid.burst{i}") for i, t in enumerate(("hi", "Ada Provisions", "Ada"))]
    await asyncio.gather(*(whatsapp.process_inbound(m) for m in burst))
    user = (await db_session.execute(select(User))).scalar_one()
    assert (user.business_name, user.firstname) == ("Ada Provisions", "Ada")

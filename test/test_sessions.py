"""
Server-side sessions: login/logout, expiry, instant revocation, what's stored, and the
CSRF (cross-site request) check that cookie logins need.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from authentication.auth import (
    SESSION_COOKIE, end_all_sessions, hash_password, token_hash, verify_password,
)
from authentication.settings import settings
from db import User, UserSession

PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def ada(db_session):
    user = User(firstname="Ada", lastname="Obi", email="ada@example.com", password=hash_password(PASSWORD),
                business_name="Ada Provisions")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def logged_in(new_browser, email="ada@example.com"):
    browser = await new_browser()
    response = await browser.post("/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return browser


async def only_session(db) -> UserSession:
    return (await db.execute(select(UserSession))).scalar_one()


# ---------- Passwords ----------

def test_password_hashing():
    hashed = hash_password("s3cret-pass")
    assert hashed != "s3cret-pass" and verify_password("s3cret-pass", hashed)
    assert not verify_password("wrong", hashed)
    assert hash_password("s3cret-pass") != hashed  # salted


# ---------- Logging in ----------

async def test_login_sets_a_locked_down_cookie(new_browser, ada):
    browser = await new_browser()
    response = await browser.post("/login", json={"email": "ada@example.com", "password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert response.json() == {"logged_in": True}
    assert (await browser.get("/api/v1/me")).json()["business_name"] == "Ada Provisions"


async def test_cookie_is_secure_when_configured(new_browser, ada, monkeypatch):
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    response = await (await new_browser()).post("/login", json={"email": "ada@example.com", "password": PASSWORD})
    assert "Secure" in response.headers["set-cookie"]


async def test_only_a_hash_of_the_token_is_stored(new_browser, ada, db_session):
    browser = await logged_in(new_browser)
    token = browser.cookies[SESSION_COOKIE]
    stored = await only_session(db_session)
    assert stored.token_hash == token_hash(token) and token not in stored.token_hash


@pytest.mark.parametrize("email, password", [("ada@example.com", "wrong-password"), ("nobody@example.com", PASSWORD)])
async def test_bad_credentials_are_the_same_401(new_browser, ada, email, password):
    response = await (await new_browser()).post("/login", json={"email": email, "password": password})
    assert response.status_code == 401 and response.json() == {"detail": "Invalid credentials"}


async def test_logging_in_again_replaces_the_old_session(new_browser, ada, db_session):
    """No session fixation: a login always gets a brand-new token."""
    browser = await logged_in(new_browser)
    old = browser.cookies[SESSION_COOKIE]
    await browser.post("/login", json={"email": "ada@example.com", "password": PASSWORD})
    assert browser.cookies[SESSION_COOKIE] != old

    stale = await new_browser()
    stale.cookies.set(SESSION_COOKIE, old)
    assert (await stale.get("/api/v1/me")).status_code == 401
    assert (await only_session(db_session)).token_hash == token_hash(browser.cookies[SESSION_COOKIE])


# ---------- Logging out ----------

async def test_logout_ends_the_session_immediately(new_browser, ada, db_session):
    browser = await logged_in(new_browser)
    token = browser.cookies[SESSION_COOKIE]

    response = await browser.post("/logout")

    assert f'{SESSION_COOKIE}=""' in response.headers["set-cookie"] and "Path=/" in response.headers["set-cookie"]
    replay = await new_browser()
    replay.cookies.set(SESSION_COOKIE, token)  # even a copied cookie is dead now
    assert (await replay.get("/api/v1/me")).status_code == 401
    assert (await db_session.execute(select(UserSession))).first() is None


async def test_logout_without_a_session_is_fine(new_browser):
    assert (await (await new_browser()).post("/logout")).status_code == 200


async def test_ending_all_sessions_logs_out_every_browser_at_once(new_browser, ada, db_session):
    phone, laptop = await logged_in(new_browser), await logged_in(new_browser)
    assert await end_all_sessions(db_session, ada.id) == 2
    assert (await phone.get("/api/v1/me")).status_code == 401
    assert (await laptop.get("/api/v1/me")).status_code == 401


async def test_ending_all_sessions_leaves_other_users_alone(new_browser, ada, db_session):
    db_session.add(User(firstname="B", lastname="C", email="b@example.com", password=hash_password(PASSWORD),
                        business_name="B"))
    await db_session.commit()
    bola = await logged_in(new_browser, email="b@example.com")
    await logged_in(new_browser)
    await end_all_sessions(db_session, ada.id)
    assert (await bola.get("/api/v1/me")).status_code == 200


# ---------- Expiry ----------

async def test_idle_session_expires(new_browser, ada, db_session):
    browser = await logged_in(new_browser)
    session = await only_session(db_session)
    session.last_seen_at = datetime.now(timezone.utc) - timedelta(days=settings.SESSION_IDLE_DAYS, minutes=1)
    await db_session.commit()
    assert (await browser.get("/api/v1/me")).status_code == 401
    assert (await db_session.execute(select(UserSession))).first() is None  # cleaned up


async def test_session_expires_at_the_hard_limit_even_if_used(new_browser, ada, db_session):
    browser = await logged_in(new_browser)
    session = await only_session(db_session)
    session.created_at = datetime.now(timezone.utc) - timedelta(days=settings.SESSION_MAX_DAYS, minutes=1)
    await db_session.commit()
    assert (await browser.get("/api/v1/me")).status_code == 401


async def test_using_a_session_keeps_it_alive(new_browser, ada, db_session):
    browser = await logged_in(new_browser)
    session = await only_session(db_session)
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    session.last_seen_at = two_hours_ago
    await db_session.commit()

    await browser.get("/api/v1/me")

    await db_session.refresh(session)
    last_seen = session.last_seen_at.replace(tzinfo=session.last_seen_at.tzinfo or timezone.utc)
    assert last_seen > two_hours_ago + timedelta(hours=1)


# ---------- CSRF: refuse writes triggered by other websites ----------

@pytest.mark.parametrize("headers, allowed", [
    ({}, True),                                                          # non-browser client
    ({"Origin": "http://test"}, True),                                   # our own page
    ({"Origin": "https://evil.example"}, False),                         # another site's page
    ({"Referer": "https://evil.example/page"}, False),                   # no Origin, foreign Referer
    ({"Referer": "http://test/"}, True),
    ({"Origin": "null"}, False),                                         # sandboxed frame
])
async def test_cross_site_writes_are_refused(new_browser, ada, headers, allowed):
    browser = await logged_in(new_browser)
    response = await browser.post("/api/v1/clarify", json={"transaction_id": 999, "amount": 1}, headers=headers)
    if allowed:
        assert response.status_code != 403  # reaches the endpoint (404: no such entry)
    else:
        assert response.status_code == 403 and response.json() == {"detail": "Cross-site request refused"}


async def test_configured_origin_is_allowed(new_browser, ada, monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://app.example.com")
    browser = await logged_in(new_browser)
    response = await browser.post("/api/v1/clarify", json={"transaction_id": 999, "amount": 1},
                                  headers={"Origin": "https://app.example.com"})
    assert response.status_code != 403


async def test_cross_site_login_is_refused_too(new_browser, ada):
    """Login CSRF: another site mustn't be able to log a visitor into an account."""
    response = await (await new_browser()).post("/login", json={"email": "ada@example.com", "password": PASSWORD},
                                                headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


async def test_reading_is_never_blocked(new_browser, ada):
    browser = await logged_in(new_browser)
    assert (await browser.get("/api/v1/me", headers={"Origin": "https://evil.example"})).status_code == 200

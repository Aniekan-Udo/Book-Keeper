"""
Integration tests for the session endpoints.

ADAPT: `from main import app` — update to your actual entrypoint.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from main import app
from db import get_db


@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    from db import get_db as get_db_dep
    app.dependency_overrides[get_db_dep] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------- Login ----------

@pytest.mark.asyncio
async def test_login_with_correct_credentials_succeeds(client, test_user):
    response = await client.post("/login", json={
        "email": test_user.email,
        "password": "correct-horse-battery-staple",
    })
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    # refresh token should be set as an httpOnly cookie, not just returned
    assert "refresh_token" in response.cookies


@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(client, test_user):
    response = await client.post("/login", json={
        "email": test_user.email,
        "password": "definitely-wrong",
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_with_nonexistent_email_returns_401_not_404(client):
    """
    Security property: a nonexistent email must return the SAME error
    as a wrong password, so the endpoint doesn't leak which emails
    are registered.
    """
    response = await client.post("/login", json={
        "email": "nobody@example.com",
        "password": "whatever",
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_response_never_contains_password_hash(client, test_user):
    response = await client.post("/login", json={
        "email": test_user.email,
        "password": "correct-horse-battery-staple",
    })
    assert "password" not in response.text


# ---------- Refresh ----------

@pytest.mark.asyncio
async def test_refresh_without_cookie_returns_401(client):
    response = await client.post("/refresh")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_valid_cookie_issues_new_access_token(client, test_user):
    login_response = await client.post("/login", json={
        "email": test_user.email,
        "password": "correct-horse-battery-staple",
    })
    old_access_token = login_response.json()["access_token"]

    refresh_response = await client.post("/refresh")
    assert refresh_response.status_code == 200
    new_access_token = refresh_response.json()["access_token"]

    assert new_access_token != old_access_token


@pytest.mark.asyncio
async def test_refresh_reuse_of_rotated_token_returns_401(client, test_user):
    """
    After a successful refresh, the OLD refresh cookie value (captured
    before rotation) should no longer work if replayed — this is the
    reuse-detection path.
    """
    login_response = await client.post("/login", json={
        "email": test_user.email,
        "password": "correct-horse-battery-staple",
    })
    old_refresh_cookie = login_response.cookies.get("refresh_token")

    # Use it once — this rotates it out
    await client.post("/refresh")

    # Replay the OLD cookie value directly
    client.cookies.set("refresh_token", old_refresh_cookie)
    reuse_response = await client.post("/refresh")
    assert reuse_response.status_code == 401


# ---------- Logout ----------

@pytest.mark.asyncio
async def test_logout_clears_cookie_and_revokes_token(client, test_user):
    await client.post("/login", json={
        "email": test_user.email,
        "password": "correct-horse-battery-staple",
    })

    logout_response = await client.post("/logout")
    assert logout_response.status_code == 200

    # A refresh attempt after logout must fail — the token was revoked
    refresh_response = await client.post("/refresh")
    assert refresh_response.status_code == 401


@pytest.mark.asyncio
async def test_logout_without_any_session_does_not_error(client):
    # Logout should be safe to call even with no active session/cookie
    response = await client.post("/logout")
    assert response.status_code == 200
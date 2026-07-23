"""
Tests for the DB-backed refresh token lifecycle: storing, validating,
revoking single tokens, and revoking all of a user's tokens.

Requires db_session fixture from conftest.py — uses a real (test) DB
since this logic's whole job is correct interaction with stored rows.
"""
import pytest
from datetime import timedelta

from authentication.auth import (
    TokenData,
    store_refresh_token,
    is_refresh_token_valid,
    revoke_refresh_token,
    revoke_all_user_tokens,
)


@pytest.mark.asyncio
async def test_store_and_validate_refresh_token(db_session, test_user):
    data = TokenData(user_id=str(test_user.id), jti="jti-1", expires_delta=timedelta(days=7))
    await store_refresh_token(db_session, data)

    is_valid = await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti="jti-1"))
    assert is_valid is True


@pytest.mark.asyncio
async def test_missing_token_is_invalid(db_session, test_user):
    is_valid = await is_refresh_token_valid(db_session, 
        TokenData(user_id=str(test_user.id), jti="never-stored")
    )
    assert is_valid is False


@pytest.mark.asyncio
async def test_revoked_token_is_invalid(db_session, test_user):
    data = TokenData(user_id=str(test_user.id), jti="jti-2", expires_delta=timedelta(days=7))
    await store_refresh_token(db_session, data)

    await revoke_refresh_token(db_session, TokenData(user_id=str(test_user.id), jti="jti-2"))

    is_valid = await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti="jti-2"))
    assert is_valid is False


@pytest.mark.asyncio
async def test_expired_token_is_invalid(db_session, test_user):
    # Store a token that's already expired — negative timedelta puts
    # expires_at in the past immediately.
    data = TokenData(user_id=str(test_user.id), jti="jti-expired", expires_delta=timedelta(seconds=-1))
    await store_refresh_token(db_session, data)

    is_valid = await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti="jti-expired"))
    assert is_valid is False


@pytest.mark.asyncio
async def test_revoke_nonexistent_token_does_not_raise(db_session, test_user):
    # Revoking a token that doesn't exist should be a no-op, not a crash —
    # relevant for the logout flow, which revokes best-effort.
    await revoke_refresh_token(db_session, TokenData(user_id=str(test_user.id), jti="never-existed"))
    # No assertion needed beyond "this didn't raise"


@pytest.mark.asyncio
async def test_revoke_all_user_tokens_kills_every_session(db_session, test_user):
    # Simulate three active sessions (e.g. three devices)
    for jti in ["device-1", "device-2", "device-3"]:
        await store_refresh_token(db_session,
            TokenData(user_id=str(test_user.id), jti=jti, expires_delta=timedelta(days=7))
        )

    await revoke_all_user_tokens(db_session, TokenData(user_id=str(test_user.id)))

    for jti in ["device-1", "device-2", "device-3"]:
        is_valid = await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti=jti))
        assert is_valid is False, f"{jti} should have been revoked"


@pytest.mark.asyncio
async def test_revoke_all_user_tokens_does_not_affect_other_users(db_session, test_user, test_recipient):
    await store_refresh_token(db_session,
        TokenData(user_id=str(test_user.id), jti="user-a-session", expires_delta=timedelta(days=7))
    )
    await store_refresh_token(db_session,
        TokenData(user_id=str(test_recipient.id), jti="user-b-session", expires_delta=timedelta(days=7))
    )

    await revoke_all_user_tokens(db_session, TokenData(user_id=str(test_user.id)))

    assert await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti="user-a-session")) is False
    assert await is_refresh_token_valid(db_session, TokenData(user_id=str(test_recipient.id), jti="user-b-session")) is True


@pytest.mark.asyncio
async def test_reuse_of_rotated_token_is_detected_as_invalid(db_session, test_user):
    """
    This is the core security property of rotation: once a refresh
    token has been used (and thus revoked/rotated), presenting it
    again must be rejected — that rejection is what the /refresh
    endpoint uses as its signal to revoke all sessions.
    """
    data = TokenData(user_id=str(test_user.id), jti="original-jti", expires_delta=timedelta(days=7))
    await store_refresh_token(db_session, data)

    # First use: rotate it out (this is what /refresh does on success)
    await revoke_refresh_token(db_session, TokenData(user_id=str(test_user.id), jti="original-jti"))

    # Second use of the same jti — must now be invalid
    is_valid = await is_refresh_token_valid(db_session, TokenData(user_id=str(test_user.id), jti="original-jti"))
    assert is_valid is False
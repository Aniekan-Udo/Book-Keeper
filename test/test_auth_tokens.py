"""
Unit tests for pure token/password logic in authentication/auth.py.
No DB dependency — these test the functions that only touch JWTs and
password hashing in isolation.
"""
import pytest
from datetime import timedelta
from fastapi import HTTPException

from authentication.auth import (
    hash_password,
    verify_password,
    create_access_token,
    verify_token,
    create_refresh_token,
)


# ---------- Password hashing ----------

def test_verify_password_correct():
    hashed = hash_password("mysecretpassword")
    assert verify_password("mysecretpassword", hashed) is True


def test_verify_password_incorrect():
    hashed = hash_password("mysecretpassword")
    assert verify_password("wrongpassword", hashed) is False


def test_hash_password_is_salted():
    # Same password, hashed twice, must produce different output —
    # confirms Argon2 salting is actually happening.
    hash1 = hash_password("mysecretpassword")
    hash2 = hash_password("mysecretpassword")
    assert hash1 != hash2
    # but both still verify correctly against the original password
    assert verify_password("mysecretpassword", hash1)
    assert verify_password("mysecretpassword", hash2)


def test_hash_password_empty_string_does_not_crash():
    # Edge case: empty password shouldn't raise — validation for
    # "password must not be empty" belongs at the request-schema
    # level, not here, but hash_password itself should be robust.
    hashed = hash_password("")
    assert verify_password("", hashed) is True
    assert verify_password("not-empty", hashed) is False


# ---------- Access tokens ----------

def test_create_and_verify_access_token():
    token = create_access_token({"sub": "user123"})
    user_id = verify_token(token)
    assert user_id == "user123"


def test_expired_access_token_raises_401():
    token = create_access_token({"sub": "user123"}, expires_delta=timedelta(seconds=-1))
    with pytest.raises(HTTPException) as exc_info:
        verify_token(token)
    assert exc_info.value.status_code == 401


def test_tampered_access_token_raises_401():
    token = create_access_token({"sub": "user123"})
    tampered = token[:-4] + "abcd"  # corrupt the signature
    with pytest.raises(HTTPException) as exc_info:
        verify_token(tampered)
    assert exc_info.value.status_code == 401


def test_access_token_missing_sub_raises_401():
    # A token that decodes fine but has no "sub" claim should still
    # be rejected — verify_token must not return None silently.
    token = create_access_token({})  # no "sub"
    with pytest.raises(HTTPException) as exc_info:
        verify_token(token)
    assert exc_info.value.status_code == 401


# ---------- Refresh tokens ----------

def test_create_refresh_token_returns_token_and_jti():
    token, jti = create_refresh_token({"sub": "user123"})
    assert isinstance(token, str)
    assert isinstance(jti, str)
    assert len(jti) > 0


def test_refresh_token_has_correct_type_claim():
    from jose import jwt
    from authentication.settings import settings

    token, jti = create_refresh_token({"sub": "user123"})
    payload = jwt.decode(token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
    assert payload["type"] == "refresh"
    assert payload["jti"] == jti
    assert payload["sub"] == "user123"


def test_access_token_and_refresh_token_are_not_interchangeable():
    """
    Critical security property: an access token must never be usable
    where a refresh token is expected, and vice versa. This is what
    the "type" claim exists to enforce.
    """
    from jose import jwt
    from authentication.settings import settings

    access = create_access_token({"sub": "user123"})
    refresh, _ = create_refresh_token({"sub": "user123"})

    access_payload = jwt.decode(access, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
    refresh_payload = jwt.decode(refresh, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])

    assert access_payload.get("type") != "refresh"
    assert refresh_payload.get("type") == "refresh"

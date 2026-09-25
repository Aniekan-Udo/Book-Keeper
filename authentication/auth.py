"""Passwords and server-side sessions.

A login is a random token in an httpOnly cookie; the server keeps only its SHA-256 in
`sessions`. Every request looks the session up, so logging out — here, or by replying
LOGOUT on WhatsApp — takes effect on the very next request. Page scripts can't read the
cookie, and CSRF is handled by SameSite=Lax plus the Origin check in `main.py`.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response, status
from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from db import User, UserSession, get_db_session

password_hash = PasswordHash.recommended()

SESSION_COOKIE = "bk_session"
# don't write last_seen_at on every request; once an hour is plenty for a 30-day idle limit
TOUCH_INTERVAL = timedelta(hours=1)


def hash_password(password):
    """ Hash a plain password """
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """ Verify a plain password against a hashed password """
    return password_hash.verify(plain_password, hashed_password)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


def session_expired(session: UserSession, now: datetime) -> bool:
    return (now - _aware(session.last_seen_at) > timedelta(days=settings.SESSION_IDLE_DAYS)
            or now - _aware(session.created_at) > timedelta(days=settings.SESSION_MAX_DAYS))


async def start_session(db: AsyncSession, request: Request, response: Response, user_id: int) -> dict:
    """Log this browser in as `user_id`: a fresh token every time (no session fixation),
    replacing any session the browser already had."""
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        await db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(old)))
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=token_hash(token), user_id=user_id))
    await db.commit()
    response.set_cookie(
        key=SESSION_COOKIE, value=token, httponly=True, secure=settings.COOKIE_SECURE, samesite="lax",
        max_age=settings.SESSION_MAX_DAYS * 24 * 60 * 60, path="/",
    )
    return {"logged_in": True}


async def end_session(db: AsyncSession, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
        await db.commit()
    # path must match start_session's or the browser keeps the cookie
    response.delete_cookie(key=SESSION_COOKIE, path="/")


async def end_all_sessions(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    await db.commit()
    return result.rowcount


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db_session)) -> User:
    """The logged-in user for this request, from the session cookie — never from anything
    in the request body."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _unauthorized()
    row = (await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash(token))
    )).first()
    if row is None:
        raise _unauthorized()
    session, user = row
    now = datetime.now(timezone.utc)
    if session_expired(session, now):
        await db.delete(session)
        await db.commit()
        raise _unauthorized()
    if now - _aware(session.last_seen_at) > TOUCH_INTERVAL:
        session.last_seen_at = now
        await db.commit()
        await db.refresh(user)  # the commit expired it; callers read its attributes
    return user

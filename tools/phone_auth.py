"""Proving someone owns a phone number, using WhatsApp in reverse.

Instead of us texting a code (which needs SMS or an approved template), the website shows
a code and the person sends "LOGIN 483920" / "LINK 483920" to our WhatsApp number. A
message *from* a number is proof of holding it. Each challenge names the number it
expects, so a code sent from any other phone does nothing — guessing codes is pointless.

Used for: logging in on the web without a password, and connecting a WhatsApp number to
an account made on the website. Never reveals on the website whether a number has an
account; that's only said on WhatsApp, to the number's owner.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from db import PhoneChallenge, User
from tools.debt_collection import business_name
from tools.phones import normalize_nigerian_phone

CHALLENGE_TTL = timedelta(minutes=10)
CODE_MESSAGE = re.compile(r"^\s*(login|link)\s+(\d{6})\s*[.!]?\s*$", re.IGNORECASE)

INVALID_CODE_REPLY = "That code isn't valid or has expired. Please get a new one on the website."
NO_ACCOUNT_REPLY = ("No Book-Keeper account uses this WhatsApp number yet. "
                    "Send me \"hi\" to create one — it takes a minute.")
LOGGED_IN_REPLY = "✅ You're now logged in on the website. If this wasn't you, reply LOGOUT to sign out everywhere."
NUMBER_TAKEN_REPLY = "This WhatsApp number is already connected to a different Book-Keeper account."


class ChallengeError(ValueError):
    """Phrased for the user."""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


def whatsapp_link(message: str) -> str | None:
    number = (settings.WHATSAPP_DISPLAY_NUMBER or "").lstrip("+")
    return f"https://wa.me/{number}?text={quote(message)}" if number else None


async def start_challenge(db: AsyncSession, purpose: str, raw_phone: str, user_id: int | None = None) -> dict:
    try:
        phone = normalize_nigerian_phone(raw_phone)
    except ValueError as e:
        raise ChallengeError(str(e))
    if purpose == "link":
        user = await db.get(User, user_id)
        if user.phone == phone:
            raise ChallengeError("That number is already connected to your account.")

    now = datetime.now(timezone.utc)
    while True:  # a code must be unambiguous among this number's open challenges
        code = f"{secrets.randbelow(10 ** 6):06d}"
        clash = (await db.execute(
            select(PhoneChallenge.id).where(PhoneChallenge.expected_phone == phone, PhoneChallenge.code == code,
                                            PhoneChallenge.status == "pending", PhoneChallenge.expires_at > now)
        )).first()
        if clash is None:
            break

    challenge = PhoneChallenge(public_id=secrets.token_urlsafe(24), code=code, purpose=purpose,
                               expected_phone=phone, user_id=user_id if purpose == "link" else None,
                               status="pending", expires_at=now + CHALLENGE_TTL)
    db.add(challenge)
    message = f"{purpose.upper()} {code}"
    result = {"challenge_id": challenge.public_id, "code": code, "message": message,
              "whatsapp_link": whatsapp_link(message), "expires_in": int(CHALLENGE_TTL.total_seconds())}
    await db.commit()
    return result


def parse_code_message(text: str) -> tuple[str, str] | None:
    match = CODE_MESSAGE.match(text or "")
    return (match.group(1).lower(), match.group(2)) if match else None


async def complete_from_whatsapp(db: AsyncSession, wa_from: str, purpose: str, code: str) -> str:
    """Handle "LOGIN 123456" / "LINK 123456" sent from `wa_from`. Returns the WhatsApp reply."""
    phone = "+" + wa_from
    now = datetime.now(timezone.utc)
    candidates = (await db.execute(
        select(PhoneChallenge)
        .where(PhoneChallenge.purpose == purpose, PhoneChallenge.code == code,
               PhoneChallenge.expected_phone == phone, PhoneChallenge.status == "pending")
        .order_by(PhoneChallenge.id.desc())
    )).scalars().all()
    challenge = next((c for c in candidates if _aware(c.expires_at) > now), None)
    if challenge is None:
        return INVALID_CODE_REPLY

    owner = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()

    if purpose == "login":
        if owner is None:
            challenge.status, challenge.rejection = "rejected", "No account uses this WhatsApp number yet."
            await db.commit()
            return NO_ACCOUNT_REPLY
        challenge.status, challenge.user_id = "approved", owner.id
        await db.commit()
        return LOGGED_IN_REPLY

    # link
    if owner is not None and owner.id != challenge.user_id:
        challenge.status, challenge.rejection = "rejected", "That WhatsApp number is already connected to another account."
        await db.commit()
        return NUMBER_TAKEN_REPLY
    user = await db.get(User, challenge.user_id)
    user.phone = phone
    challenge.status = "approved"
    reply = (f"✅ This WhatsApp number is now connected to {business_name(user)}. "
             "You can keep your books right here — just tell me what you sold or spent.")
    try:
        await db.commit()
    except IntegrityError:  # another account claimed the number at the same moment
        await db.rollback()
        return NUMBER_TAKEN_REPLY
    return reply


async def poll(db: AsyncSession, public_id: str) -> dict:
    """What the waiting web page sees. An approved challenge can be collected once."""
    challenge = (await db.execute(
        select(PhoneChallenge).where(PhoneChallenge.public_id == public_id)
    )).scalar_one_or_none()
    if challenge is None or challenge.status == "consumed":
        return {"status": "expired"}
    if challenge.status == "rejected":
        return {"status": "rejected", "message": challenge.rejection}
    if challenge.status == "pending":
        if _aware(challenge.expires_at) <= datetime.now(timezone.utc):
            return {"status": "expired"}
        return {"status": "pending"}
    result = {"status": "approved", "purpose": challenge.purpose, "user_id": challenge.user_id}
    challenge.status = "consumed"
    await db.commit()
    return result

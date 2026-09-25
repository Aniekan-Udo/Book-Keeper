"""Sign-up inside WhatsApp: two questions, then an account tied to the sender's number.

The WhatsApp number is the account: only the person holding it can message from it, so
no email, password or verification code is needed. Nothing is created until both
questions are answered, so a stray "hi" doesn't make an account.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from db import OnboardingSession, User

SESSION_TTL = timedelta(hours=24)  # an abandoned sign-up starts over after this
CANCEL_WORDS = {"stop", "cancel", "no", "no thanks"}

ASK_NAME = "Thanks! And what should I call you?"
TYPE_PLEASE = "Please type your answer 🙂"
CANCELLED = "No problem — message me any time to start again."
ALREADY_REGISTERED = "This number already has a Book-Keeper account. Just tell me what you sold or spent."


def _welcome() -> str:
    privacy = (f"By replying, you agree to our privacy notice: {settings.PRIVACY_POLICY_URL}"
               if settings.PRIVACY_POLICY_URL else
               "Your records are private and only used to run Book-Keeper for you.")
    return ("Welcome to Book-Keeper! 📒 I keep your shop's books from simple WhatsApp messages and voice notes — "
            "sales, expenses, and who owes you.\n\n"
            "Let's set you up — just 2 quick questions. What's the name of your business?\n\n" + privacy)


def _done(first_name: str) -> str:
    text = (f"You're set up, {first_name}! 🎉\n\nTell me what you sell or spend, like "
            "\"I sell 2 bags rice to Emeka 90k, he pay 50k\". You can also send voice notes or forward bank alerts.")
    if settings.APP_PUBLIC_URL:
        text += (f"\n\nTo see your books on a computer, go to {settings.APP_PUBLIC_URL.rstrip('/')} "
                 "and choose \"Log in with WhatsApp\".")
    return text


def looks_like_transaction(text: str) -> bool:
    """A first message worth recording once the account exists ("I sold rice 5k"), as
    opposed to a greeting ("hi", "good morning")."""
    return bool(re.search(r"\d", text)) and len(text.split()) >= 3


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


@dataclass
class OnboardingResult:
    replies: list[str]
    user_id: int | None = None      # set once the account is created
    replay: str | None = None       # their first message, to record now that they have an account


async def handle(db: AsyncSession, wa_from: str, message: dict) -> OnboardingResult:
    now = datetime.now(timezone.utc)
    session = (await db.execute(
        select(OnboardingSession).where(OnboardingSession.wa_from == wa_from)
    )).scalar_one_or_none()
    if session is not None and now - _aware(session.updated_at) > SESSION_TTL:
        await db.delete(session)
        await db.flush()
        session = None

    text = message["text"]["body"] if message.get("type") == "text" else None

    if session is None:
        first = text if text and looks_like_transaction(text) else None
        db.add(OnboardingSession(wa_from=wa_from, step="business_name", first_message=first, updated_at=now))
        await db.commit()
        return OnboardingResult([_welcome()])

    if text is None:
        return OnboardingResult([TYPE_PLEASE])
    if text.strip().lower().rstrip(".!") in CANCEL_WORDS:
        await db.delete(session)
        await db.commit()
        return OnboardingResult([CANCELLED])

    if session.step == "business_name":
        name = _clean(text, 100)
        if len(name) < 2:
            return OnboardingResult(["What's the name of your business?"])
        session.business_name, session.step, session.updated_at = name, "name", now
        await db.commit()
        return OnboardingResult([ASK_NAME])

    # step == "name"
    full_name = _clean(text, 100)
    if len(full_name) < 2:
        return OnboardingResult([ASK_NAME])
    first_name, _, last_name = full_name.partition(" ")
    user = User(firstname=first_name[:50], lastname=last_name[:50], business_name=session.business_name,
                phone="+" + wa_from)
    replay = session.first_message
    db.add(user)
    await db.delete(session)
    try:
        await db.flush()
        user_id = user.id
        await db.commit()
    except IntegrityError:  # the number got attached to an account meanwhile (e.g. via Connect WhatsApp)
        await db.rollback()
        return OnboardingResult([ALREADY_REGISTERED])
    return OnboardingResult([_done(first_name)], user_id=user_id, replay=replay)

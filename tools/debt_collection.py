"""Payment reminders: the trader says "remind Mama Chidi", we draft the exact message,
the trader confirms in a later message, and it goes to the debtor over WhatsApp.

Everything that protects the debtor and the business number is enforced here, not left
to the model: confirmation in a separate turn, opt-outs, a per-debtor cooldown and a
daily cap. A reminder reaches someone who never signed up for anything, so err on the
side of not sending.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from channels import whatsapp_api
from db import ConversationTurn, Reminder, ReminderOptOut, TransactionORM, User
from tools.contacts import find_contact, name_key
from tools.formatting import format_naira
from tools.phones import display_phone

DRAFT_TTL = timedelta(minutes=30)
TRADER_WINDOW = timedelta(hours=24)  # WhatsApp only allows free-form messages within this
STOP_WORDS = {"stop", "unsubscribe", "stop reminders", "stop reminder", "no more reminders"}
MAX_DETAIL_ITEMS = 3

# must match the template approved in Meta Business Manager, with {{1}}..{{4}} in place of these
# (each variable used once, in order — see Documentation.MD)
REMINDER_TEMPLATE_TEXT = (
    "Hello {name}, this is a friendly reminder from {business}. You have an outstanding balance of "
    "{amount} for {details}. Please contact them to arrange payment. Thank you! Reply STOP to stop these reminders."
)


class ReminderRefused(ValueError):
    """A reason not to draft/send, phrased for the trader."""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; values are UTC


def business_name(user: User) -> str:
    return user.business_name or f"{user.firstname} {user.lastname}"


async def open_debts(db: AsyncSession, user_id: int, party: str) -> list[TransactionORM]:
    return list((await db.execute(
        select(TransactionORM)
        .where(
            TransactionORM.user_id == user_id,
            TransactionORM.is_deleted.is_(False),
            TransactionORM.status == "complete",
            TransactionORM.type.in_(("sale", "loan_given")),
            TransactionORM.outstanding > 0,
            func.lower(func.trim(TransactionORM.party)) == name_key(party),
        )
        .order_by(TransactionORM.date, TransactionORM.id)
    )).scalars().all())


def debt_details(debts: list[TransactionORM]) -> str:
    parts = []
    for d in debts[:MAX_DETAIL_ITEMS]:
        if d.type == "loan_given":
            what = "money borrowed"
        else:
            qty = f"{d.quantity:g} {d.unit or ''} ".replace("  ", " ") if d.quantity else ""
            what = f"{qty}{d.item or 'goods'}".strip()
        parts.append(f"{what} ({d.date.strftime('%b %d')})")
    if len(debts) > MAX_DETAIL_ITEMS:
        parts.append(f"and {len(debts) - MAX_DETAIL_ITEMS} more")
    return ", ".join(parts)


async def is_opted_out(db: AsyncSession, phone: str) -> bool:
    return (await db.execute(select(ReminderOptOut.id).where(ReminderOptOut.phone == phone))).first() is not None


async def opt_out(db: AsyncSession, phone: str) -> None:
    if not await is_opted_out(db, phone):
        db.add(ReminderOptOut(phone=phone))
        await db.commit()


async def _check_limits(db: AsyncSession, user_id: int, party: str, phone: str) -> None:
    if await is_opted_out(db, phone):
        raise ReminderRefused(f"{party} has asked not to receive reminders, so I can't send one.")

    now = datetime.now(timezone.utc)
    last_sent = (await db.execute(
        select(func.max(Reminder.sent_at))
        .where(Reminder.user_id == user_id, Reminder.phone == phone, Reminder.status == "sent")
    )).scalar()
    cooldown = timedelta(days=settings.REMINDER_COOLDOWN_DAYS)
    if last_sent and now - _aware(last_sent) < cooldown:
        next_allowed = (_aware(last_sent) + cooldown).strftime("%A %b %d")
        raise ReminderRefused(f"{party} got a reminder recently. To keep it friendly, the next one can go on {next_allowed}.")

    sent_today = (await db.execute(
        select(func.count(Reminder.id))
        .where(Reminder.user_id == user_id, Reminder.status == "sent", Reminder.sent_at >= now - timedelta(days=1))
    )).scalar()
    if sent_today >= settings.REMINDER_DAILY_LIMIT:
        raise ReminderRefused(f"You've sent {sent_today} reminders in the last 24 hours, which is the daily limit. Try again tomorrow.")


async def draft_reminder(db: AsyncSession, user: User, party: str) -> dict:
    if not settings.reminders_enabled:
        raise ReminderRefused("Payment reminders aren't switched on yet for this account.")

    debts = await open_debts(db, user.id, party)
    if not debts:
        raise ReminderRefused(f"{party} doesn't owe you anything on record, so there's nothing to remind them about.")

    contact = await find_contact(db, user.id, party)
    if contact is None:
        raise ReminderRefused(f"I don't have {party}'s phone number yet. What's their number?")

    await _check_limits(db, user.id, party, contact.phone)

    total = sum((d.outstanding for d in debts), Decimal("0"))
    details = debt_details(debts)
    reminder = Reminder(user_id=user.id, party=contact.name, phone=contact.phone, amount=total,
                        details=details, status="draft")
    db.add(reminder)
    await db.flush()
    result = {
        "reminder_id": reminder.id,
        "to": f"{contact.name} ({display_phone(contact.phone)})",
        "message": REMINDER_TEMPLATE_TEXT.format(name=contact.name, business=business_name(user),
                                                 amount=format_naira(total), details=details),
        "next_step": "Show the user who this goes to and the exact message, and ask them to confirm. "
                     "Only send it with confirm_reminder_tool after they say yes in their next message.",
    }
    await db.commit()
    return result


async def confirm_reminder(db: AsyncSession, user: User, reminder_id: int, not_before: datetime) -> dict:
    """Send a drafted reminder. `not_before` is when the current request started: a draft
    made during this same request can't be confirmed, so the trader always sees it first."""
    reminder = await db.get(Reminder, reminder_id)
    now = datetime.now(timezone.utc)
    if reminder is None or reminder.user_id != user.id or reminder.status != "draft":
        raise ReminderRefused("I couldn't find that reminder draft. Ask me to remind them again.")
    created = _aware(reminder.created_at)
    if now - created > DRAFT_TTL:
        raise ReminderRefused("That draft has expired. Ask me to remind them again and I'll prepare a fresh one.")
    if created >= not_before:
        raise ReminderRefused("The user hasn't seen this reminder yet. Show it to them and wait for their yes.")

    await _check_limits(db, user.id, reminder.party, reminder.phone)

    # the debt may have been paid or changed since the draft was shown
    debts = await open_debts(db, user.id, reminder.party)
    total = sum((d.outstanding for d in debts), Decimal("0"))
    if total == 0:
        raise ReminderRefused(f"{reminder.party} no longer owes anything, so I didn't send it.")
    if total != reminder.amount:
        raise ReminderRefused(f"{reminder.party}'s balance changed to {format_naira(total)} since the draft. "
                              "Ask me to remind them again so you can check the new message.")

    party, phone, amount = reminder.party, reminder.phone, reminder.amount
    try:
        wa_id = await whatsapp_api.send_template(
            to=phone.removeprefix("+"),
            name=settings.WHATSAPP_REMINDER_TEMPLATE,
            language=settings.WHATSAPP_REMINDER_TEMPLATE_LANG,
            params=[party, business_name(user), format_naira(amount), reminder.details],
        )
    except whatsapp_api.WhatsAppSendError:
        reminder.status = "failed"
        await db.commit()
        raise ReminderRefused(f"WhatsApp didn't accept the reminder to {party}. The number may not be on WhatsApp.")

    reminder.status = "sent"
    reminder.sent_at = now
    reminder.wa_message_id = wa_id
    await db.commit()
    return {"sent": True, "to": f"{party} ({display_phone(phone)})", "amount": format_naira(amount),
            "reminder_id": reminder_id, "party": party}


async def pending_drafts(db: AsyncSession, user_id: int) -> list[str]:
    since = datetime.now(timezone.utc) - DRAFT_TTL
    drafts = (await db.execute(
        select(Reminder)
        .where(Reminder.user_id == user_id, Reminder.status == "draft", Reminder.created_at >= since)
        .order_by(Reminder.created_at.desc())
        .limit(3)
    )).scalars().all()
    return [f"#{r.id}: to {r.party} for {format_naira(r.amount)}" for r in drafts]


# ── replies from debtors ─────────────────────────────────────────────────────

def is_stop_request(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in STOP_WORDS


async def latest_reminder_to(db: AsyncSession, phone: str) -> Reminder | None:
    return (await db.execute(
        select(Reminder)
        .where(Reminder.phone == phone, Reminder.status == "sent")
        .order_by(Reminder.sent_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def trader_reachable(db: AsyncSession, user_id: int) -> bool:
    """Whether we may message the trader free-form: they wrote to us on WhatsApp in the last 24 hours."""
    last = (await db.execute(
        select(func.max(ConversationTurn.created_at))
        .where(ConversationTurn.user_id == user_id, ConversationTurn.role == "user",
               ConversationTurn.channel == "whatsapp")
    )).scalar()
    return bool(last) and datetime.now(timezone.utc) - _aware(last) < TRADER_WINDOW

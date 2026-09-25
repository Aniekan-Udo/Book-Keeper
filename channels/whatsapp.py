"""WhatsApp Cloud API channel: users keep their books by chatting with the business number.

Setup (Meta developer dashboard → your app → WhatsApp → Configuration):
  Callback URL:  https://<your domain>/webhooks/whatsapp
  Verify token:  the value of WHATSAPP_VERIFY_TOKEN
  Subscribe to:  messages

Flow: Meta POSTs a signed webhook → we check the signature, remember each message id
(Meta redelivers; the unique id stops double-recording), reply 200 straight away, and
answer in a background task — the LLM can take longer than Meta waits.
"""
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from channels.whatsapp_api import (  # noqa: F401 (re-exported for callers and tests)
    MAX_VOICE_BYTES, MediaTooLarge, _split, download_media, send_text,
)
from db import SessionLocal, User, WhatsAppInboundMessage, get_db_session
from tools import alerts, debt_collection, transcribe as voice
from tools.assistant import respond
from tools.router import LLMUnavailable
from utils import logger

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

HEARD_PREVIEW_CHARS = 200


@asynccontextmanager
async def _new_session():
    async with SessionLocal() as session:
        yield session

# background work outlives the request's session, so it opens its own; tests swap this out
session_factory = _new_session


# ── inbound ──────────────────────────────────────────────────────────────────

def _require_enabled() -> None:
    if not settings.whatsapp_enabled:
        raise HTTPException(status_code=404, detail="WhatsApp channel is not configured")


def signature_is_valid(raw_body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.WHATSAPP_APP_SECRET.get_secret_value().encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def signup_message(wa_from: str) -> str:
    phone = "+" + wa_from
    if settings.APP_PUBLIC_URL:
        link = f"{settings.APP_PUBLIC_URL.rstrip('/')}/?signup=1&phone={quote(phone)}"
        where = f"Create your free account with this number here:\n{link}"
    else:
        where = f"Create your account on the Book-Keeper website using this number ({phone})."
    return (f"Welcome to Book-Keeper! 📒\n\nI keep your shop's books from simple messages like "
            f"\"I sell 3 bags rice to Emeka, 45k, he pay 30k\".\n\n{where}\n\nThen come back and message me here.")


UNSUPPORTED_REPLY = ("I can read text, voice notes, and screenshots of bank alerts or POS receipts — "
                     "but not other files yet. Please type or say what happened.")
IMAGE_NOT_ALERT_REPLY = ("I can only read screenshots of bank alerts, transfer notifications or POS receipts. "
                         "For anything else, please type or say what happened.")
IMAGE_TOO_LARGE_REPLY = "That image is too large for me. Please send a normal screenshot."
IMAGE_FAILED_REPLY = "I couldn't read that image just now. Please try again, or paste the alert text."
MAX_IMAGE_BYTES = 5_000_000
VOICE_TOO_LONG_REPLY = "That voice note is a bit long for me. Please send shorter ones — one or two sales at a time."
VOICE_FORMAT_REPLY = "I couldn't open that audio. Please record it as a normal WhatsApp voice note, or type it."
VOICE_INAUDIBLE_REPLY = "I couldn't hear that clearly. Please send the voice note again somewhere quieter, or type it."
VOICE_FAILED_REPLY = "I couldn't process that voice note just now. Please try again, or type it."
BUSY_REPLY = "I'm a bit busy right now. Please send that again in a couple of minutes."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again."


class MediaProblem(Exception):
    """Carries the reply to send when a voice note or image can't be used."""
    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


async def voice_note_text(audio: dict) -> tuple[str, str]:
    """Returns (text for the assistant, what was heard — echoed back to the user)."""
    try:
        voice.normalize_mime(audio.get("mime_type", ""))
    except voice.UnsupportedAudio:
        raise MediaProblem(VOICE_FORMAT_REPLY)

    try:
        data = await download_media(audio["id"])
    except MediaTooLarge:
        raise MediaProblem(VOICE_TOO_LONG_REPLY)
    except httpx.HTTPError as e:
        logger.error(f"WhatsApp media download failed: {e}")
        raise MediaProblem(VOICE_FAILED_REPLY)

    try:
        heard = await voice.transcribe(data, audio["mime_type"])
    except Exception as e:
        logger.error(f"Voice note transcription failed: {e}", exc_info=True)
        raise MediaProblem(VOICE_FAILED_REPLY)

    if not heard.audible or not heard.transcript.strip():
        raise MediaProblem(VOICE_INAUDIBLE_REPLY)
    return voice.as_message(heard), heard.transcript.strip()


async def alert_screenshot_text(image: dict) -> str:
    """A screenshot of a bank alert / POS receipt, as a message for the assistant."""
    try:
        data = await download_media(image["id"], max_bytes=MAX_IMAGE_BYTES)
    except MediaTooLarge:
        raise MediaProblem(IMAGE_TOO_LARGE_REPLY)
    except httpx.HTTPError as e:
        logger.error(f"WhatsApp media download failed: {e}")
        raise MediaProblem(IMAGE_FAILED_REPLY)
    try:
        text = await alerts.read_alert_image(data, image.get("mime_type", ""))
    except Exception as e:
        logger.error(f"Alert screenshot reading failed: {e}", exc_info=True)
        raise MediaProblem(IMAGE_FAILED_REPLY)
    if not text:
        raise MediaProblem(IMAGE_NOT_ALERT_REPLY)
    caption = image.get("caption", "").strip()
    return f"(forwarded alert screenshot) {text}" + (f"\n(caption: {caption})" if caption else "")


def _with_heard(heard: str, reply: str) -> str:
    """Show the user what we understood from their voice note, so a mishearing is obvious."""
    if len(heard) > HEARD_PREVIEW_CHARS:
        heard = heard[:HEARD_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
    return f"🎤 “{heard}”\n\n{reply}"


OPTED_OUT_REPLY = ("Done — you won't receive any more payment reminders from this number. "
                   "For questions about a balance, please contact the business directly.")


def debtor_auto_reply(business: str) -> str:
    return (f"Thanks for your message. This number sends payment reminders for {business} and can't reply to "
            f"messages. We've passed your message on to {business}. Reply STOP to stop these reminders.")


async def _handle_reminder_recipient(db, wa_from: str, message: dict, registered: bool) -> bool:
    """Replies from people who received a payment reminder. Returns True if handled."""
    phone = "+" + wa_from
    reminder = await debt_collection.latest_reminder_to(db, phone)
    if reminder is None:
        return False
    text = message.get("text", {}).get("body", "") if message.get("type") == "text" else ""

    if debt_collection.is_stop_request(text):
        await debt_collection.opt_out(db, phone)
        await send_text(wa_from, OPTED_OUT_REPLY)
        return True
    if registered:
        return False  # a trader who also got a reminder: their messages are bookkeeping as usual

    trader = await db.get(User, reminder.user_id)
    business = debt_collection.business_name(trader)
    await send_text(wa_from, debtor_auto_reply(business))
    # pass it on, if WhatsApp lets us message the trader right now (24-hour window)
    if trader.phone and await debt_collection.trader_reachable(db, trader.id):
        said = f"“{text}”" if text else "a message I can't show here (voice note or media)"
        await send_text(trader.phone.removeprefix("+"), f"💬 {reminder.party} replied to your reminder: {said}")
    return True


async def process_inbound(message: dict) -> None:
    """Answer one inbound WhatsApp message. Runs after the webhook has already returned 200."""
    wa_from = message["from"]
    async with session_factory() as db:
        user_id = (await db.execute(select(User.id).where(User.phone == "+" + wa_from))).scalar_one_or_none()

        if await _handle_reminder_recipient(db, wa_from, message, registered=user_id is not None):
            return

        if user_id is None:
            await send_text(wa_from, signup_message(wa_from))
            return

        heard = None
        if message.get("type") == "text":
            text = message["text"]["body"]
        elif message.get("type") == "audio":
            try:
                text, heard = await voice_note_text(message["audio"])
            except MediaProblem as problem:
                await send_text(wa_from, problem.reply)
                return
        elif message.get("type") == "image":
            try:
                text = await alert_screenshot_text(message["image"])
            except MediaProblem as problem:
                await send_text(wa_from, problem.reply)
                return
        else:
            await send_text(wa_from, UNSUPPORTED_REPLY)
            return

        try:
            answer = await respond(db, user_id, text, channel="whatsapp")
            reply = _with_heard(heard, answer.reply) if heard else answer.reply
        except LLMUnavailable:
            reply = BUSY_REPLY
        except Exception as e:
            logger.error(f"WhatsApp message failed: {e}", wamid=message.get("id"), user_id=user_id, exc_info=True)
            reply = ERROR_REPLY
        await send_text(wa_from, reply)


def _inbound_messages(payload: dict) -> list[dict]:
    """Pull user messages out of a webhook payload, ignoring delivery/read statuses and
    anything addressed to a different business number."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            if value.get("metadata", {}).get("phone_number_id") != settings.WHATSAPP_PHONE_NUMBER_ID:
                continue
            messages.extend(m for m in value.get("messages", []) if m.get("id") and m.get("from"))
    return messages


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
):
    """Meta calls this once when the webhook URL is saved in the dashboard."""
    _require_enabled()
    expected = settings.WHATSAPP_VERIFY_TOKEN.get_secret_value()
    if mode != "subscribe" or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Verification failed")
    return challenge


@router.post("")
async def receive_webhook(request: Request, background: BackgroundTasks,
                          db: AsyncSession = Depends(get_db_session)):
    _require_enabled()
    raw = await request.body()
    if not signature_is_valid(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for message in _inbound_messages(payload):
        db.add(WhatsAppInboundMessage(wamid=message["id"], wa_from=message["from"]))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # a redelivery of a message we've already taken
            continue
        background.add_task(process_inbound, message)

    # always 200 once the signature checks out, or Meta keeps redelivering
    return {"status": "ok"}

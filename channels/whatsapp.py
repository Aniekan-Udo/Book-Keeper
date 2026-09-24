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
from db import SessionLocal, User, WhatsAppInboundMessage, get_db_session
from tools.assistant import respond
from tools.router import LLMUnavailable
from utils import logger

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

MAX_MESSAGE_CHARS = 4096  # WhatsApp's limit for one text message


@asynccontextmanager
async def _new_session():
    async with SessionLocal() as session:
        yield session

# background work outlives the request's session, so it opens its own; tests swap this out
session_factory = _new_session


# ── outbound ─────────────────────────────────────────────────────────────────

def _split(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks + [text] if text else chunks


async def send_text(to: str, body: str) -> None:
    """Send a plain text message to a WhatsApp number (digits only, e.g. 2348031234567)."""
    url = (f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
           f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages")
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN.get_secret_value()}"}
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split(body):
            response = await client.post(url, headers=headers, json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            })
            if response.status_code >= 400:
                logger.error("WhatsApp send failed", status=response.status_code, body=response.text[:500])
                return


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


UNSUPPORTED_REPLY = {
    "audio": "I can't listen to voice notes yet — please type it for now. Voice notes are coming soon!",
}
DEFAULT_UNSUPPORTED_REPLY = "I can only read text messages for now. Please type what happened."
BUSY_REPLY = "I'm a bit busy right now. Please send that again in a couple of minutes."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again."


async def process_inbound(message: dict) -> None:
    """Answer one inbound WhatsApp message. Runs after the webhook has already returned 200."""
    wa_from = message["from"]
    async with session_factory() as db:
        user_id = (await db.execute(select(User.id).where(User.phone == "+" + wa_from))).scalar_one_or_none()

        if user_id is None:
            await send_text(wa_from, signup_message(wa_from))
            return

        if message.get("type") != "text":
            await send_text(wa_from, UNSUPPORTED_REPLY.get(message.get("type"), DEFAULT_UNSUPPORTED_REPLY))
            return

        try:
            answer = await respond(db, user_id, message["text"]["body"], channel="whatsapp")
            reply = answer.reply
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

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
from tools import transcribe as voice
from tools.assistant import respond
from tools.router import LLMUnavailable
from utils import logger

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

MAX_MESSAGE_CHARS = 4096  # WhatsApp's limit for one text message
# ~2–3 minutes of WhatsApp voice (Opus, ~16 kbps); longer notes are rarely one transaction
MAX_VOICE_BYTES = 400_000
HEARD_PREVIEW_CHARS = 200


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
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split(body):
            response = await client.post(url, headers=_graph_headers(), json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            })
            if response.status_code >= 400:
                logger.error("WhatsApp send failed", status=response.status_code, body=response.text[:500])
                return


class MediaTooLarge(Exception):
    pass


def _graph_headers() -> dict:
    return {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN.get_secret_value()}"}


async def download_media(media_id: str, max_bytes: int = MAX_VOICE_BYTES) -> bytes:
    """Fetch an inbound media file. Two steps: the media id resolves to a short-lived
    URL, which must itself be fetched with the same access token."""
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{media_id}",
                                headers=_graph_headers())
        meta.raise_for_status()
        info = meta.json()
        if int(info.get("file_size") or 0) > max_bytes:
            raise MediaTooLarge()
        media = await client.get(info["url"], headers=_graph_headers())
        media.raise_for_status()
        if len(media.content) > max_bytes:
            raise MediaTooLarge()
        return media.content


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


UNSUPPORTED_REPLY = "I can read text and listen to voice notes, but not photos or files yet. Please type or say what happened."
VOICE_TOO_LONG_REPLY = "That voice note is a bit long for me. Please send shorter ones — one or two sales at a time."
VOICE_FORMAT_REPLY = "I couldn't open that audio. Please record it as a normal WhatsApp voice note, or type it."
VOICE_INAUDIBLE_REPLY = "I couldn't hear that clearly. Please send the voice note again somewhere quieter, or type it."
VOICE_FAILED_REPLY = "I couldn't process that voice note just now. Please try again, or type it."
BUSY_REPLY = "I'm a bit busy right now. Please send that again in a couple of minutes."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again."


class VoiceNoteProblem(Exception):
    """Carries the reply to send when a voice note can't be turned into text."""
    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


async def voice_note_text(audio: dict) -> tuple[str, str]:
    """Returns (text for the assistant, what was heard — echoed back to the user)."""
    try:
        voice.normalize_mime(audio.get("mime_type", ""))
    except voice.UnsupportedAudio:
        raise VoiceNoteProblem(VOICE_FORMAT_REPLY)

    try:
        data = await download_media(audio["id"])
    except MediaTooLarge:
        raise VoiceNoteProblem(VOICE_TOO_LONG_REPLY)
    except httpx.HTTPError as e:
        logger.error(f"WhatsApp media download failed: {e}")
        raise VoiceNoteProblem(VOICE_FAILED_REPLY)

    try:
        heard = await voice.transcribe(data, audio["mime_type"])
    except Exception as e:
        logger.error(f"Voice note transcription failed: {e}", exc_info=True)
        raise VoiceNoteProblem(VOICE_FAILED_REPLY)

    if not heard.audible or not heard.transcript.strip():
        raise VoiceNoteProblem(VOICE_INAUDIBLE_REPLY)
    return voice.as_message(heard), heard.transcript.strip()


def _with_heard(heard: str, reply: str) -> str:
    """Show the user what we understood from their voice note, so a mishearing is obvious."""
    if len(heard) > HEARD_PREVIEW_CHARS:
        heard = heard[:HEARD_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
    return f"🎤 “{heard}”\n\n{reply}"


async def process_inbound(message: dict) -> None:
    """Answer one inbound WhatsApp message. Runs after the webhook has already returned 200."""
    wa_from = message["from"]
    async with session_factory() as db:
        user_id = (await db.execute(select(User.id).where(User.phone == "+" + wa_from))).scalar_one_or_none()

        if user_id is None:
            await send_text(wa_from, signup_message(wa_from))
            return

        heard = None
        if message.get("type") == "text":
            text = message["text"]["body"]
        elif message.get("type") == "audio":
            try:
                text, heard = await voice_note_text(message["audio"])
            except VoiceNoteProblem as problem:
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

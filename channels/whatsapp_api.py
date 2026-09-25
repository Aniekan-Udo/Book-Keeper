"""Outbound calls to the WhatsApp Cloud API. No app logic here, so any module (the
webhook, the reminder tools) can send without import cycles."""
import re

import httpx

from authentication.settings import settings
from utils import logger

MAX_MESSAGE_CHARS = 4096  # WhatsApp's limit for one text message
# ~2–3 minutes of WhatsApp voice (Opus, ~16 kbps); longer notes are rarely one transaction
MAX_VOICE_BYTES = 400_000


class MediaTooLarge(Exception):
    pass


class WhatsAppSendError(Exception):
    pass


def _graph_headers() -> dict:
    return {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN.get_secret_value()}"}


def _messages_url() -> str:
    return f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"


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
    """Send a plain text message to a WhatsApp number (digits only, e.g. 2348031234567).
    Only allowed within 24 hours of that person's last message to us."""
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split(body):
            response = await client.post(_messages_url(), headers=_graph_headers(), json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            })
            if response.status_code >= 400:
                logger.error("WhatsApp send failed", status=response.status_code, body=response.text[:500])
                return


def template_param(value: str) -> str:
    # Meta rejects template parameters containing newlines, tabs or 4+ consecutive spaces
    return re.sub(r"\s+", " ", value).strip()


async def send_template(to: str, name: str, language: str, params: list[str]) -> str:
    """Send an approved template message — the only kind WhatsApp allows to someone who
    hasn't messaged us in the last 24 hours. Returns the WhatsApp message id."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(_messages_url(), headers=_graph_headers(), json={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": template_param(p)} for p in params],
                }],
            },
        })
    if response.status_code >= 400:
        logger.error("WhatsApp template send failed", status=response.status_code, body=response.text[:500])
        raise WhatsAppSendError(response.text[:500])
    return response.json()["messages"][0]["id"]


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

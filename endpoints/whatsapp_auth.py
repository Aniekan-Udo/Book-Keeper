"""Log in, or connect a number, by sending a code from WhatsApp (see tools/phone_auth.py).

    POST /auth/whatsapp/login        {phone}  → code + wa.me link; no account needed to ask
    POST /auth/whatsapp/link         {phone}  → same, for the logged-in account (Connect WhatsApp)
    POST /auth/whatsapp/challenges/<id>       → pending / approved / rejected / expired;
                                               an approved login returns a session, once
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import get_current_user, issue_session
from authentication.settings import settings
from db import User, get_db_session
from tools import phone_auth
from utils import limiter

router = APIRouter(prefix="/auth/whatsapp", tags=["auth"])


class PhoneRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=20)


def _require_whatsapp() -> None:
    if not settings.whatsapp_enabled:
        raise HTTPException(status_code=404, detail="WhatsApp isn't set up on this server")


@router.get("/info")
async def whatsapp_info():
    """Lets the website show WhatsApp options only when they'll work."""
    number = (settings.WHATSAPP_DISPLAY_NUMBER or "").lstrip("+")
    enabled = settings.whatsapp_enabled and bool(number)
    return {"enabled": enabled, "number": number if enabled else None,
            "signup_link": phone_auth.whatsapp_link("Hi") if enabled else None}


@router.post("/login")
@limiter.limit("5/minute")
async def start_login(request: Request, body: PhoneRequest, db: AsyncSession = Depends(get_db_session)):
    _require_whatsapp()
    try:
        return await phone_auth.start_challenge(db, "login", body.phone)
    except phone_auth.ChallengeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/link")
@limiter.limit("5/minute")
async def start_link(request: Request, body: PhoneRequest, db: AsyncSession = Depends(get_db_session),
                     user: User = Depends(get_current_user)):
    _require_whatsapp()
    try:
        return await phone_auth.start_challenge(db, "link", body.phone, user_id=user.id)
    except phone_auth.ChallengeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/challenges/{challenge_id}")
@limiter.limit("60/minute")
async def check_challenge(request: Request, challenge_id: str, response: Response,
                          db: AsyncSession = Depends(get_db_session)):
    result = await phone_auth.poll(db, challenge_id)
    if result["status"] == "approved" and result["purpose"] == "login":
        session = await issue_session(db, response, result["user_id"])
        return {"status": "approved", **session}
    if result["status"] == "approved":
        return {"status": "approved"}
    return result

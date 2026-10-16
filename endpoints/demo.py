"""One-click login to the demo accounts (scripts/seed_demo.py), for judges and visitors.

Off unless DEMO_ACCOUNTS=true. The demo accounts hold only made-up data, and logging in
as one gives exactly what its owner would have, nothing more: no other account is
reachable this way.
"""
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import start_session
from db import User, get_db_session
from utils import limiter, logger

router = APIRouter(prefix="/demo", tags=["demo"])

# (email, business name) of the two accounts scripts/seed_demo.py creates
DAY30 = ("ngozi.day30@example.com", "Ngozi Provisions (day 30)")
DAY1 = ("ngozi.day1@example.com", "Ngozi Provisions (day 1)")

ACCOUNTS = {
    "day30": {"email": DAY30[0], "label": "Day 30: a month of memory",
              "about": "Ngozi Provisions after 30 days. The agents know her customers, their promises and habits."},
    "day1": {"email": DAY1[0], "label": "Day 1: nothing remembered yet",
             "about": "The same books, but no memory: see what the agents can't do without it."},
}


def demo_enabled() -> bool:
    return os.getenv("DEMO_ACCOUNTS", "").strip().lower() in ("1", "true", "yes", "on")


class DemoLogin(BaseModel):
    account: Literal["day30", "day1"]


@router.get("/info")
async def info(db: AsyncSession = Depends(get_db_session)):
    if not demo_enabled():
        return {"enabled": False, "accounts": []}
    emails = [a["email"] for a in ACCOUNTS.values()]
    present = set((await db.execute(select(User.email).where(User.email.in_(emails)))).scalars().all())
    return {"enabled": bool(present), "accounts": [
        {"id": key, "label": a["label"], "about": a["about"]} for key, a in ACCOUNTS.items() if a["email"] in present
    ]}


@router.post("/login")
@limiter.limit("20/minute", key_func=get_remote_address)
async def login(request: Request, body: DemoLogin, response: Response, db: AsyncSession = Depends(get_db_session)):
    if not demo_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    user = (await db.execute(select(User).where(User.email == ACCOUNTS[body.account]["email"]))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="The demo accounts haven't been set up on this server.")
    logger.info("demo.login", account=body.account, user_id=user.id)
    return await start_session(db, request, response, user.id)

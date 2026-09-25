from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from slowapi.util import get_remote_address
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import start_session, verify_password
from db import get_db_session, User
from utils import limiter


router = APIRouter()

class Login(BaseModel):
    email: EmailStr
    password: str

@router.post("/login")
@limiter.limit("5/minute", key_func=get_remote_address)
async def login(request: Request, credentials: Login, response: Response, db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(select(User).where(func.lower(User.email) == credentials.email.lower()))
    user = result.scalar_one_or_none()

    # WhatsApp-only accounts have no password: they log in with a WhatsApp code instead
    if user is None or user.password is None or not verify_password(credentials.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return await start_session(db, request, response, user.id)

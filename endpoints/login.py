from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import issue_session, verify_password
from db import get_db_session, User
from utils import limiter


router = APIRouter()

class Login(BaseModel):
    email: EmailStr
    password: str

@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, credentials: Login, response: Response, db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(select(User).where(func.lower(User.email) == credentials.email.lower()))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(credentials.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return await issue_session(db, response, user.id)

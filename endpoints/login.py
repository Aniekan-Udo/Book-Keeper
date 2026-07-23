from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from starlette.responses import Response

from authentication.auth import create_access_token, create_refresh_token, store_refresh_token, verify_password
from authentication.settings import settings

from db import get_db, User


router = APIRouter()

class TokenData(BaseModel):
    user_id: str
    expires_delta: timedelta | None = None
    jti: str = None

class Login(BaseModel):
    email: EmailStr
    password: str

@router.post("/login")
async def login(credentials: Login, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == credentials.email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(credentials.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token({"sub": str(user.id)})
    refresh_token, jti = create_refresh_token({"sub": str(user.id)})

    await store_refresh_token(db, TokenData(user_id=str(user.id), jti=jti, expires_delta=timedelta(days=7)))

    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True,
        secure=False, samesite="lax", max_age=7*24*60*60, path="/",
    )
    return {"access_token": access_token, "token_type": "bearer"}
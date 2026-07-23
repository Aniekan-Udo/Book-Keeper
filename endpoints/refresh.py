from datetime import timedelta
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends
from jose import jwt, ExpiredSignatureError, JWTError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import is_refresh_token_valid, revoke_all_user_tokens, revoke_refresh_token, \
    create_access_token, create_refresh_token, store_refresh_token
from authentication.settings import settings
from db import get_db, User
from aiocache import caches
import uvicorn
import asyncio
import uuid

from utils import limiter

router = APIRouter()

class TokenData(BaseModel):
    user_id: str
    expires_delta: timedelta | None = None
    jti: str = None

@router.post("/refresh")
@limiter.limit("10/minute")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    old_token = request.cookies.get("refresh_token")
    if not old_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    try:
        payload = jwt.decode(old_token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id, jti = payload["sub"], payload.get("jti")

    if not await is_refresh_token_valid(db, TokenData(user_id=user_id, jti=jti)):
        await revoke_all_user_tokens(db, TokenData(user_id=user_id))
        raise HTTPException(status_code=401, detail="Token reuse detected, all sessions revoked")

    await revoke_refresh_token(db, TokenData(user_id=user_id, jti=jti))

    new_access = create_access_token({"sub": user_id})
    new_refresh, new_jti = create_refresh_token({"sub": user_id})

    await store_refresh_token(db, TokenData(user_id=user_id, jti=new_jti, expires_delta=timedelta(days=7)))

    response.set_cookie(
        key="refresh_token", value=new_refresh, httponly=True,
        secure=False, samesite="lax", max_age=7*24*60*60, path="/",
    )
    return {"access_token": new_access, "token_type": "bearer"}

from fastapi import APIRouter, Request, Response, HTTPException, Depends
from jose import jwt, ExpiredSignatureError, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import (
    REFRESH_COOKIE, TokenData, is_refresh_token_valid, issue_session, revoke_all_user_tokens, revoke_refresh_token,
)
from authentication.settings import settings
from db import get_db_session
from utils import limiter

router = APIRouter()

@router.post("/refresh")
@limiter.limit("10/minute")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db_session)):
    old_token = request.cookies.get(REFRESH_COOKIE)
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
    return await issue_session(db, response, int(user_id))

from fastapi import APIRouter, Depends, Request, Response
from jose import jwt, ExpiredSignatureError, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import REFRESH_COOKIE, TokenData, clear_session_cookie, revoke_refresh_token
from authentication.settings import settings
from db import get_db_session


router = APIRouter()

@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db_session)):
    old_token = request.cookies.get(REFRESH_COOKIE)
    if old_token:
        try:
            payload = jwt.decode(old_token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
            user_id, jti = payload.get("sub"), payload.get("jti")
            if user_id and jti:
                await revoke_refresh_token(db, TokenData(user_id=user_id, jti=jti))
        except (ExpiredSignatureError, JWTError):
            pass
    clear_session_cookie(response)
    return {"message": "Logged out"}

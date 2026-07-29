from fastapi import APIRouter, Request, Response, HTTPException, status

from jose import jwt, ExpiredSignatureError, JWTError
from starlette.responses import Response

from authentication.settings import settings
from authentication.auth import revoke_refresh_token, TokenData


router = APIRouter()

from db import get_db_session
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db_session)):
    old_token = request.cookies.get("refresh_token")
    if old_token:
        try:
            payload = jwt.decode(old_token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
            user_id, jti = payload.get("sub"), payload.get("jti")
            if user_id and jti:
                await revoke_refresh_token(db, TokenData(user_id=user_id, jti=jti))
        except (ExpiredSignatureError, JWTError):
            pass
    response.delete_cookie(key="refresh_token", path="/refresh")
    return {"message": "Logged out"}
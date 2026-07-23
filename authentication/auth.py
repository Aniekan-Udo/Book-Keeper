from sqlalchemy import select
from db import get_db
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError, ExpiredSignatureError, jwt
from datetime import timedelta, datetime, timezone
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, status, Depends
from pwdlib import PasswordHash
from pydantic import BaseModel


from authentication.settings import settings
from db import RefreshToken

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
oauth2_scheme_step_up = OAuth2PasswordBearer(tokenUrl="token")

def hash_password(password):
    """ Hash a plain password """
    return password_hash.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """ Verify a plain password against a hashed password """
    return password_hash.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta : timedelta | None = None):
    """ Create an access token """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes= settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    import uuid
    to_encode.update({"exp": expire, "jti": str(uuid.uuid4())})

    encode_jwt =jwt.encode(to_encode, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)

    return encode_jwt

def create_refresh_token(data: dict, expires_delta : timedelta | None = None):
    """ Create a refresh token """
    import uuid
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=7))
    jti = str(uuid.uuid4())
    to_encode.update({"exp": expire, "type": "refresh", "jti": jti})
    token = jwt.encode(to_encode, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)
    return token, jti

def verify_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
        if payload.get("type") in ("refresh", "step_up"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        return user_id

    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired. Please log in again",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class TokenData(BaseModel):
    user_id: str
    expires_delta: timedelta | None = None
    jti: str = None

async def store_refresh_token(db: AsyncSession, data: TokenData):
    """ Store a refresh token """
    token_row = RefreshToken(
        user_id=int(data.user_id),
        jti=data.jti,
        expires_at=datetime.now(timezone.utc) + data.expires_delta,
    )
    db.add(token_row)
    await db.commit()
    return token_row

async def is_refresh_token_valid(db: AsyncSession, data: TokenData):
    """ Check if a refresh token is valid """
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == int(data.user_id),
            RefreshToken.jti == data.jti,
        )
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        return False
    if token_row.revoked:
        return False
    if token_row.expires_at < datetime.now(timezone.utc):
        return False

    return True

async def revoke_refresh_token(db: AsyncSession, data: TokenData):
    """ Revoke a refresh token """
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == int(data.user_id),
            RefreshToken.jti == data.jti,
        )
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        return

    await db.delete(token_row)
    await db.commit()


async def revoke_all_user_tokens(db: AsyncSession, data: TokenData):
    """ Revoke all refresh tokens for a user """
    await db.execute(
        delete(RefreshToken).where(RefreshToken.user_id == int(data.user_id))
    )
    await db.commit()


def create_step_up_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=3))
    to_encode.update({"exp": expire, "type": "step_up"})
    return jwt.encode(to_encode, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)


async def require_step_up(token: str = Depends(oauth2_scheme_step_up)):  # separate header/scheme, or parse from a custom header
    try:
        payload = jwt.decode(token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
    except (ExpiredSignatureError, JWTError):
        raise HTTPException(status_code=401, detail="Step-up verification required")

    if payload.get("type") != "step_up":
        raise HTTPException(status_code=401, detail="Invalid verification token")

    return payload["sub"]
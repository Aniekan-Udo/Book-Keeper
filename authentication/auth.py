import uuid
from datetime import timedelta, datetime, timezone

from fastapi import HTTPException, Response, status, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, ExpiredSignatureError, jwt
from pwdlib import PasswordHash
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.settings import settings
from db import RefreshToken, User, get_db_session

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

REFRESH_COOKIE = "refresh_token"

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

    to_encode.update({"exp": expire, "jti": str(uuid.uuid4())})

    encode_jwt =jwt.encode(to_encode, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)

    return encode_jwt

def create_refresh_token(data: dict, expires_delta : timedelta | None = None):
    """ Create a refresh token """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    jti = str(uuid.uuid4())
    to_encode.update({"exp": expire, "type": "refresh", "jti": jti})
    token = jwt.encode(to_encode, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)
    return token, jti

def verify_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY.get_secret_value(), algorithms=[settings.ALGORITHM])
        if payload.get("type") is not None:
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
    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:  # SQLite drops tzinfo; stored values are always UTC
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
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


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """The authenticated user for this request. Identity only ever comes from the
    access token — never from a user_id in the request body."""
    user_id = verify_token(token)
    user = await db.get(User, int(user_id)) if str(user_id).isdigit() else None
    if user is None:
        # token outlived the account
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


async def issue_session(db: AsyncSession, response: Response, user_id: int) -> dict:
    """Create an access token + a stored, rotating refresh token (set as an httpOnly cookie)."""
    access_token = create_access_token({"sub": str(user_id)})
    refresh_token, jti = create_refresh_token({"sub": str(user_id)})
    lifetime = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    await store_refresh_token(db, TokenData(user_id=str(user_id), jti=jti, expires_delta=lifetime))
    response.set_cookie(
        key=REFRESH_COOKIE, value=refresh_token, httponly=True,
        secure=settings.COOKIE_SECURE, samesite="lax", max_age=int(lifetime.total_seconds()), path="/",
    )
    return {"access_token": access_token, "token_type": "bearer"}


def clear_session_cookie(response: Response) -> None:
    # path must match the one used in issue_session or the browser keeps the cookie
    response.delete_cookie(key=REFRESH_COOKIE, path="/")

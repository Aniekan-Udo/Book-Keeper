from fastapi import FastAPI, Depends, HTTPException
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, result
from sqlalchemy import select
from pydantic import BaseModel

from authentication.auth import is_refresh_token_valid, revoke_all_user_tokens, revoke_refresh_token, \
    create_access_token, create_refresh_token, store_refresh_token
from authentication.settings import settings
from db import get_db_session, User
from aiocache import caches
import uvicorn
import asyncio
import uuid

class UserIn(BaseModel):
    firstname: str
    lastname: str
    email: str

caches.set_config({
    'default': {
        'cache': 'aiocache.RedisCache',
        'endpoint': 'redis',
        'port': 6379,
        'timeout': 5,
        'pool_min_size': 5,
        'pool_max_size': 50,
    }
})

cache = caches.get('default')

app = FastAPI()

@app.post('/check_balance')
async def check_balance(user: UserIn, db: AsyncSession = Depends(get_db_session)):
    key = f'{user.email}:{user.firstname}:{user.lastname}'
    cached_value = await cache.get(key)
    if cached_value:
        return cached_value

    try:
        result = await db.execute(
            select(User).where(
                User.email == user.email,
                User.firstname == user.firstname,
                User.lastname == user.lastname,
            )
        )
        get_user = result.scalar_one_or_none()
        if get_user:
            response = {"message": "User already exists!", "balance": float(get_user.balance)}
        else:
            response = {"message": "User does not exist!", "balance": float(0)}

        await cache.set(key, response, ttl=60)
        print(f"DB HIT for {user.firstname} {user.lastname}")
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
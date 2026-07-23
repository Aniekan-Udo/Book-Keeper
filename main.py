import os
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from db import get_db, User
from aiocache import caches
import uvicorn
import asyncio
import uuid

from authentication.auth import (
   oauth2_scheme, verify_token,
)

class UserIn(BaseModel):
    firstname: str
    lastname: str
    email: str

caches.set_config({
    'default': {
        'cache': 'aiocache.RedisCache',
        'endpoint': os.getenv("REDIS_HOST", "localhost"),
        'port': 6379,
        'timeout': 5,
        'pool_min_size': 5,
        'pool_max_size': 50,
    }
})

cache = caches.get('default')

async def get_current_user(
        token: str = Depends(oauth2_scheme),
        db: AsyncSession = Depends(get_db),
):
    user_id = verify_token(token)

    result = await db.execute(
        select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user
app = FastAPI()

from endpoints import login, logout, refresh, transfer, verification
app.include_router(login.router)
app.include_router(logout.router)
app.include_router(refresh.router)
app.include_router(transfer.router)
app.include_router(verification.router)

@app.post('/check_balance')
async def check_balance(user: UserIn, db: AsyncSession = Depends(get_db)):
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

@app.post('/generate_report')
async def generate_report(user:UserIn):
    lock_key = f'lock:report:{user.email}'
    result_key = f'result:report:{user.email}'
    lock_value = uuid.uuid4().hex

    cached_result = await cache.get(result_key)
    if cached_result is not None:
        return cached_result

    # Try to acquire the lock - only one result wins
    try:
        acquired = await cache.add(lock_key, lock_value, ttl=60)
    except ValueError:
        acquired = False

    if acquired:
        try:
            print(f"[{lock_value[:6]}] Doing expensive work for {user.email}")
            await  asyncio.sleep(3)
            result = {"report":f"Report for {user.email}", "generated_at":"now"}
            await cache.set(result_key, result, ttl=60)
            return result
        finally:
            await cache.delete(lock_key)

    else:
        #Someone else is already generating it - wait briefly and check for result
        for _ in range(20):
            await asyncio.sleep(0.2)
            cached_result = await cache.get(result_key)
            if cached_result is not None:
                return cached_result
        raise HTTPException(status_code=503, detail="Report generation in progress, try again shortly")



import socket
@app.get("/health")
async def health_check():
    return {"hostname":socket.gethostname(), "status": "ok"}

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8001)
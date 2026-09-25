from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import end_session
from db import get_db_session


router = APIRouter()

@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db_session)):
    await end_session(db, request, response)
    return {"message": "Logged out"}

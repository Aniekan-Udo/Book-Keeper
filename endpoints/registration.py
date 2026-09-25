from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import hash_password, issue_session
from db import get_db_session, User
from utils import limiter

router = APIRouter()


class Registration(BaseModel):
    """Web sign-up. There's no phone field on purpose: a phone number is only attached to
    an account once its owner proves it from WhatsApp ("Connect WhatsApp")."""
    firstname: str = Field(min_length=1, max_length=50)
    lastname: str = Field(min_length=1, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    business_name: str = Field(min_length=1, max_length=100)


@router.post("/register", status_code=201, tags=["registration"])
@limiter.limit("5/minute")
async def register(request: Request, reg: Registration, response: Response,
                   db: AsyncSession = Depends(get_db_session)):
    email = reg.email.lower()
    existing = (await db.execute(select(User.id).where(func.lower(User.email) == email))).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        firstname=reg.firstname.strip(), lastname=reg.lastname.strip(), email=email,
        password=hash_password(reg.password), business_name=reg.business_name.strip(),
    )
    db.add(user)
    try:
        await db.flush()
        user_id = user.id
        await db.commit()
    except IntegrityError:
        # lost a race with a concurrent sign-up using the same email
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")

    # log straight in after signing up
    return await issue_session(db, response, user_id)

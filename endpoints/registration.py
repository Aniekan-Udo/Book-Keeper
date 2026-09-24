import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select, or_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import hash_password, issue_session
from db import get_db_session, User
from utils import limiter

router = APIRouter()


def normalize_nigerian_phone(raw: str) -> str:
    """Accepts 0803..., 234803..., +234 803 ... and returns E.164 (+234803...)."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Enter a valid Nigerian phone number, e.g. 0803 123 4567")
    return "+234" + digits


class Registration(BaseModel):
    firstname: str = Field(min_length=1, max_length=50)
    lastname: str = Field(min_length=1, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    business_name: str = Field(min_length=1, max_length=100)
    phone: str | None = None

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, v: str | None) -> str | None:
        return normalize_nigerian_phone(v) if v else None


@router.post("/register", status_code=201, tags=["registration"])
@limiter.limit("5/minute")
async def register(request: Request, reg: Registration, response: Response,
                   db: AsyncSession = Depends(get_db_session)):
    email = reg.email.lower()
    clauses = [func.lower(User.email) == email] + ([User.phone == reg.phone] if reg.phone else [])
    existing = (await db.execute(select(User).where(or_(*clauses)))).scalars().first()
    if existing is not None:
        detail = "Email already registered" if existing.email.lower() == email else "Phone number already registered"
        raise HTTPException(status_code=409, detail=detail)

    user = User(
        firstname=reg.firstname.strip(), lastname=reg.lastname.strip(), email=email,
        password=hash_password(reg.password),
        business_name=reg.business_name.strip(), phone=reg.phone,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # lost a race with a concurrent sign-up using the same email/phone
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email or phone number already registered")
    await db.refresh(user)

    # log straight in after signing up
    return await issue_session(db, response, user.id)

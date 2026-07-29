router = APIRouter()

class RegisterUser(BaseModel):
    user_name: str = Field(title="User's name", description="User's name")
    email: str

class RegisterBusiness(BaseModel):
    business_name: str



@router.post("/registration/user", tags=["registration"])
async def registration(reg_user: RegisterUser, db: AsyncSession = Depends(get_db_session)):
    try:
        existing_user = await db.query(User).filter(
            (User.email == reg_user.email) | (User.user_name == reg_user.user_name)
        ).first()

    except Exception as e:
        logger.error("Database error during user lookup: %s",e)
        raise HTTPException(status_code=500, detail="Internal server error")

    if existing_user.scalar():
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        email=reg_user.email,
        password=reg_user.password,
    )

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return new_user


@router.post("/registration/business", tags=["registration"])
async def registration(reg_business: RegisterBusiness, db: AsyncSession = Depends(get_db_session)):
    try:
        existing_business = await db.query(Organization).filter(
            (Organization.name == reg_business.name)
        ).first()

    except Exception as e:
        logger.error("Database error during user lookup: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

    if existing_business.scalar():
        raise HTTPException(status_code=400, detail="Email already registered")

    new_business = Organization(
        email=reg_user.email,
        password=reg_user.password,
    )

    db.add(new_business)
    await db.commit()
    await db.refresh(new_business)

    return new_business
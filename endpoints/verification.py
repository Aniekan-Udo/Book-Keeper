from datetime import datetime, timezone, timedelta

from fastapi import Depends, HTTPException, APIRouter
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import create_step_up_token
from db import get_db_session, User, StepUpVerification
from main import get_current_user

router = APIRouter()

class BiometricAssertion(BaseModel):
    platform: str  # "ios" or "android"
    signature: str  # the cryptographic proof from the device
    device_id: str
    timestamp: datetime




@router.post("/verify_setup")
async def verify_setup(
        assertion: BiometricAssertion,
        db: AsyncSession = Depends(get_db_session),
        current_user: User = Depends(get_current_user)
):
    # verify the platform's cryptographic assertion —
    # iOS: verify against a public key registered during device enrollment
    # Android: verify the BiometricPrompt result via Play Integrity API or similar
    if not verify_platform_assertion(assertion, current_user):
        raise HTTPException(status_code=401, detail="Verification failed")

    step_up_row = StepUpVerification(
        user_id=str(current_user.id),
        method='biometric',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=3)
    )
    db.add(step_up_row)
    await db.commit()


    return {"step_up_token": create_step_up_token({"sub": str(current_user.id)}, expires_delta=timedelta(minutes=3))}


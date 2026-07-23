from decimal import Decimal
import asyncio

from fastapi import Depends, HTTPException, APIRouter, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import require_step_up
from db import User, get_db, IdempotencyRecord, Transaction, TransactionStatus
from main import get_current_user
from utils import limiter

router = APIRouter()


class TransferRequest(BaseModel):
    recipient_email: str
    amount: Decimal
    idempotency_key: str

transfer_lock = asyncio.Lock()

@router.post("/transfer")
@limiter.limit("5/minute")
async def transfer(
    request: Request,
    transfer_req: TransferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    step_up_sub: str = Depends(require_step_up)
):
    async with transfer_lock:
        if step_up_sub != str(current_user.id):
            raise HTTPException(status_code=403, detail="Step-up token mismatch")

        # 1. Idempotency Check
        existing = await db.execute(
            select(IdempotencyRecord).where(IdempotencyRecord.key == transfer_req.idempotency_key)
        )
        if record := existing.scalar_one_or_none():
            return record.response

        in_transaction = db.in_transaction()
        context = db.begin_nested() if in_transaction else db.begin()
        async with context:
            # 2. Lock sender
            sender_result = await db.execute(
                select(User).where(User.id == current_user.id).with_for_update()
            )
            sender = sender_result.scalar_one_or_none()

            # 3. Check Balance
            if sender.balance < transfer_req.amount:
                raise HTTPException(status_code=400, detail="Insufficient balance")

            # 4. Lock Recipient
            recipient_result = await db.execute(
                select(User).where(User.email == transfer_req.recipient_email).with_for_update()
            )
            recipient = recipient_result.scalar_one_or_none()
            if not recipient:
                raise HTTPException(status_code=404, detail="Recipient not found")

            if transfer_req.amount <= 0:
                raise HTTPException(status_code=400, detail="Amount must be positive")

            # 5. Move Money
            sender.balance -= transfer_req.amount
            recipient.balance += transfer_req.amount

            response = {
                "message": "Transfer successful",
                "reference": f"TXN-{uuid.uuid4().hex}",
                "amount": str(transfer_req.amount),
                "recipient": transfer_req.recipient_email,
            }

            # 6. Record Transaction & Idempotency
            db.add(Transaction(
                reference=response["reference"],
                sender_id=sender.id,
                recipient_id=recipient.id,
                amount=transfer_req.amount,
                sender_balance_after=sender.balance,
                recipient_balance_after=recipient.balance,
                status="completed",
                idempotency_key=transfer_req.idempotency_key,
            ))
            
            db.add(IdempotencyRecord(
                key=transfer_req.idempotency_key, 
                user_id=int(current_user.id), 
                response=response,
            ))

        return response
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import date
from llm import Models
from db import get_db
from db import TransactionORM  # or wherever that class actually lives
from sqlalchemy.ext.asyncio import AsyncSession  # add this import if not already there



class Transaction(BaseModel):
    type: Literal["sale", "expense", "loan_taken", "loan_given", "debt_repayment"]
    party: Optional[str] = None
    item: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    amount: Optional[float] = None
    direction: Literal["in", "out"]
    note: Optional[str] = None
    date: Optional[date] = None
    status: Literal["complete", "needs_amount"] = "complete"

class ExtractionResult(BaseModel):
    transactions: list[Transaction]

async def create_conversation(db, model: Models) -> tuple[list[Transaction], list[dict]]:
    try:
        tool_call = await model.tool_call()
    except Exception as err:
        raise err  # logger.error(err)

    result = ExtractionResult.model_validate(tool_call["args"])

    from datetime import date as date_type

    db_rows = [
        TransactionORM(
            type=t.type, party=t.party, item=t.item, quantity=t.quantity,
            unit=t.unit, amount=t.amount, direction=t.direction,
            note=t.note,
            date=t.date or date_type.today(),  
            status=t.status,
        )
        for t in result.transactions
    ]

    try:
        db.add_all(db_rows)
        await db.flush()   # assigns primary keys without ending the transaction
    except Exception:
        await db.rollback()
        raise

    clarifications = [
        {"transaction_id": row.id, "message": f"You mentioned {t.item or t.type} — what was the amount?"}
        for t, row in zip(result.transactions, db_rows)
        if t.status == "needs_amount"
    ]

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return result.transactions, clarifications


async def resolve_clarification(db: AsyncSession, transaction_id: int, amount: float):
    txn = await db.get(TransactionORM, transaction_id)
    txn.amount = amount
    txn.status = "complete"
    await db.commit()
    await db.refresh(txn)
    return txn

if __name__ == "main":
    db = Database()

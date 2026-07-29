from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import date
from db import get_db

from pydantic import BaseModel, model_validator
from datetime import date

class GenerateDate(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        return self


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
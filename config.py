from datetime import date

from pydantic import BaseModel, model_validator


class GenerateDate(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        return self

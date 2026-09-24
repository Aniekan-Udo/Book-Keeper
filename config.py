from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, model_validator

# users' "today" is Nigerian time, whatever timezone the server runs in
BUSINESS_TZ = ZoneInfo("Africa/Lagos")


def business_today() -> date:
    return datetime.now(BUSINESS_TZ).date()


class GenerateDate(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        return self

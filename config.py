import re
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, WithJsonSchema, model_validator

# users' "today" is Nigerian time, whatever timezone the server runs in
BUSINESS_TZ = ZoneInfo("Africa/Lagos")


def business_today() -> date:
    return datetime.now(BUSINESS_TZ).date()


# formats models and bank alerts actually produce; day-first, as dates are written in Nigeria
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
                 "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d %b, %Y")
_RELATIVE_DAYS = {"today": 0, "yesterday": -1, "tomorrow": 1}
_TIME_PART = re.compile(r"[T\s]+\d{1,2}:\d{2}.*$")


def parse_loose_date(value) -> date | None:
    """A date from a model or a bank alert, in whatever shape it arrived. Unreadable → None,
    so one oddly written date never fails a whole message."""
    if value is None or isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in _RELATIVE_DAYS:
        return business_today() + timedelta(days=_RELATIVE_DAYS[text.lower()])
    text = _TIME_PART.sub("", text).strip().rstrip(",")  # "25-Sep-2026 10:15" → "25-Sep-2026"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Use for every date a model fills in. Its schema says "string, YYYY-MM-DD" rather than
# format:date because some providers (Groq) validate tool arguments against the schema on
# their side and reject the whole call over a date like "25-Sep-2026 10:15".
LooseDate = Annotated[
    date | None,
    BeforeValidator(parse_loose_date),
    WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "A calendar date, written YYYY-MM-DD"}),
]


class GenerateDate(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        return self

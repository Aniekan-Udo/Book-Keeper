# tools/formatting.py
from datetime import date
from decimal import Decimal


def format_naira(amount: float | Decimal) -> str:
    return f"₦{amount:,.2f}"


def _fmt(d: date) -> str:
    return d.strftime('%B %d, %Y')


def describe_period(start: date | None, end: date | None) -> str:
    if not start and not end:
        return "all time"
    if start == end:
        return f"on {_fmt(start)}"
    if not end:
        return f"since {_fmt(start)}"
    if not start:
        return f"up to {_fmt(end)}"
    return f"from {_fmt(start)} to {_fmt(end)}"

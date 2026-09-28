"""Dates filled in by models (and read off bank alerts) come in many shapes. Some
providers (Groq) reject a whole tool call when a date doesn't match the schema, so the
schema says "string" and the app does the reading — seen for real: "25-Sep-2026 10:15"."""
from datetime import date, timedelta

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from config import business_today, parse_loose_date
from tools.alerts import ParsedAlert
from tools.clean_version import build_tools
from tools.ledger import TransactionInput


@pytest.mark.parametrize("value, expected", [
    ("2026-09-25", date(2026, 9, 25)),
    ("25-Sep-2026 10:15", date(2026, 9, 25)),          # what the alert parser actually sent
    ("25/09/2026", date(2026, 9, 25)),                 # day first, as written in Nigeria
    ("25-09-2026", date(2026, 9, 25)),
    ("2026-09-25T10:15:00", date(2026, 9, 25)),
    ("Sep 25, 2026", date(2026, 9, 25)),
    ("25 September 2026", date(2026, 9, 25)),
    (date(2026, 9, 25), date(2026, 9, 25)),
    ("", None),
    (None, None),
    ("sometime next week", None),                      # unreadable: dropped, never an error
])
def test_parse_loose_date(value, expected):
    assert parse_loose_date(value) == expected


def test_relative_days_follow_lagos_time():
    assert parse_loose_date("yesterday") == business_today() - timedelta(days=1)
    assert parse_loose_date("Today") == business_today()


def test_models_fill_dates_leniently():
    t = TransactionInput(type="sale", direction="in", date="25-Sep-2026 10:15", due_date="30/09/2026")
    assert (t.date, t.due_date) == (date(2026, 9, 25), date(2026, 9, 30))
    assert ParsedAlert(is_alert=True, kind="credit_alert", occurred_on="Sep 25, 2026").occurred_on == date(2026, 9, 25)


def test_no_tool_asks_the_provider_to_enforce_a_date_format():
    for t in build_tools(None, 1, []):
        schema = str(convert_to_openai_tool(t))
        assert "'format': 'date'" not in schema, t.name

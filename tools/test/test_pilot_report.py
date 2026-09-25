"""The pilot review report: usage numbers and the exchanges flagged for review."""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from db import ConversationTurn, Model, User
from tools.assistant import FALLBACK_REPLY
from tools.ledger import TransactionInput, delete_transactions, persist_transactions
from tools.pilot_report import build_report, is_correction


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        yield session
    await engine.dispose()


@pytest.mark.parametrize("text, expected", [
    ("cancel that", True),
    ("no be 50k, na 45k", True),
    ("sorry it was 45k", True),
    ("that's wrong", True),
    ("I sold 2 bags rice to Emeka 90k", False),
    ("who owe me?", False),
    ("send me my notebook total", False),
])
def test_correction_detection(text, expected):
    assert is_correction(text) is expected


async def say(db, user_id, *exchange, channel="whatsapp", ago=timedelta(hours=1)):
    at = datetime.now(timezone.utc) - ago
    for i, (role, content) in enumerate(zip(("user", "assistant") * len(exchange), exchange)):
        db.add(ConversationTurn(user_id=user_id, role=role, content=content, channel=channel,
                                created_at=at + timedelta(seconds=i)))
    await db.commit()


async def test_report_counts_usage_and_flags_mistakes(db):
    db.add(User(id=1, firstname="Ada", lastname="", business_name="Ada Provisions", phone="+2348031234567"))
    await db.commit()
    await persist_transactions(db, [TransactionInput(type="expense", direction="out", item="fuel", amount=50000)], 1)
    await delete_transactions(db, 1)
    await say(db, 1, "Paid 50k fuel", "Recorded ₦50,000 on fuel.",
              "no be 50k, na 45k", "Fixed: ₦45,000.",
              "(voice note) I sell rice", FALLBACK_REPLY)

    report = await build_report(db, days=7)

    assert "| Ada Provisions (Ada) | 1 | 3 | 3 | 1 | 0 | 1 | 1 | 1 | 1 |" in report
    assert "### Trader corrected the bot — Ada Provisions (Ada)" in report
    assert "> **Trader:** Paid 50k fuel" in report and "> **Trader:** no be 50k, na 45k" in report
    assert "### Bot couldn't handle it" in report
    assert "Messages needing a fix (corrections + gave up): 67%" in report


async def test_old_activity_is_outside_the_window(db):
    db.add(User(id=1, firstname="Ada", lastname="", business_name="Ada Provisions"))
    await db.commit()
    await say(db, 1, "cancel that", "Done.", ago=timedelta(days=30))
    report = await build_report(db, days=7)
    assert "Nothing flagged." in report and "(no activity)" in report

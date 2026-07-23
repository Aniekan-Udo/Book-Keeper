# drop_tables.py
import asyncio
from sqlalchemy import text
from db import engine

async def drop_broken_tables():
    async with engine.begin() as conn:
        await conn.execute(text(
            "DROP TABLE IF EXISTS refresh_tokens, step_up_verifications, "
            "transactions, idempotency_records, alembic_version CASCADE"
        ))
    print("Dropped.")

asyncio.run(drop_broken_tables())
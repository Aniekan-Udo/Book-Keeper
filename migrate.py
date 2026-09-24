"""Bring the database schema up to date. Safe to run on every deploy.

- Fresh database: create the current schema and mark it as fully migrated.
- Database built by the old create_tables.py (tables but no alembic_version): mark it as
  at the first migration, then apply the rest (they check what already exists).
- Already under Alembic: apply any new migrations.

    uv run python migrate.py
"""
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db import engine, init_models

BASELINE = "4df41001e977"


async def _existing_tables() -> set[str]:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    await engine.dispose()
    return tables


def main() -> None:
    cfg = Config(str(Path(__file__).with_name("alembic.ini")))
    tables = asyncio.run(_existing_tables())

    if "alembic_version" in tables:
        print("Applying pending migrations")
        command.upgrade(cfg, "head")
    elif "user" in tables:
        print("Existing database without migration history: stamping baseline, then upgrading")
        command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")
    else:
        print("Fresh database: creating schema")
        asyncio.run(init_models())
        command.stamp(cfg, "head")


if __name__ == "__main__":
    main()

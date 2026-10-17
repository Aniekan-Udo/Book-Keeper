"""Party identity: one row per person in a trader's books, found by name key.

Entries (business_transactions.party_id), bank names (party_aliases.party_id) and the
graph's Party nodes (pg_id) all point at this id, so "who is this" is answered in one
place instead of by comparing lower-cased strings wherever it's needed.
"""
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from db import Party


def party_key(name: str | None) -> str | None:
    """The grouping the ledger has always used: lower-cased and trimmed."""
    return name.strip().lower() if name and name.strip() else None


async def party_id_for(db: AsyncSession, user_id: int, name: str | None) -> int | None:
    """The id for this name, created on first use (None for no name). The latest spelling
    becomes the display name. Safe if two writers race: insert-or-ignore, then read."""
    key = party_key(name)
    if key is None:
        return None
    display = name.strip()[:120]
    row = (await db.execute(select(Party).where(Party.user_id == user_id, Party.key == key))).scalar_one_or_none()
    if row is not None:
        if row.name != display:
            row.name = display
        return row.id
    dialect = postgresql if db.bind.dialect.name == "postgresql" else sqlite
    await db.execute(dialect.insert(Party).values(user_id=user_id, key=key[:120], name=display)
                     .on_conflict_do_nothing(index_elements=["user_id", "key"]))
    return (await db.execute(select(Party.id).where(Party.user_id == user_id, Party.key == key))).scalar_one()


async def find_party_id(db: AsyncSession, user_id: int, name: str | None) -> int | None:
    """The id for this name if the trader has one, without creating it."""
    key = party_key(name)
    if key is None:
        return None
    return (await db.execute(select(Party.id).where(Party.user_id == user_id, Party.key == key))).scalar_one_or_none()

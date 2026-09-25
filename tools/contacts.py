from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import Contact
from tools.phones import normalize_nigerian_phone


def name_key(name: str) -> str:
    # same normalisation used to group debts by party (see tools/ledger._party_key)
    return name.strip().lower()


async def save_contact(db: AsyncSession, user_id: int, name: str, raw_phone: str) -> Contact:
    """Create or update the phone number for one of the user's customers/suppliers.
    Raises ValueError for a number that isn't a valid Nigerian phone. Doesn't commit."""
    phone = normalize_nigerian_phone(raw_phone)
    key = name_key(name)
    contact = (await db.execute(
        select(Contact).where(Contact.user_id == user_id, Contact.name_key == key)
    )).scalar_one_or_none()
    if contact is None:
        contact = Contact(user_id=user_id, name=name.strip(), name_key=key, phone=phone)
        db.add(contact)
    else:
        contact.phone = phone
    await db.flush()
    return contact


async def find_contact(db: AsyncSession, user_id: int, name: str) -> Contact | None:
    return (await db.execute(
        select(Contact).where(Contact.user_id == user_id, Contact.name_key == name_key(name))
    )).scalar_one_or_none()

"""parties: a stable id for each person in a trader's books

Revision ID: e2f6a8c0b417
Revises: 9a3c5e7b1d20
Create Date: 2026-10-17

Creates `parties`, adds `party_id` to entries and bank names, and backfills both from the
names already there (grouped the way the ledger always grouped them: lower-cased, trimmed;
the latest spelling becomes the display name).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2f6a8c0b417'
down_revision: Union[str, Sequence[str], None] = '9a3c5e7b1d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKFILL = [
    # from entries: one party per (trader, name key), the newest spelling as the name
    """
    INSERT INTO parties (user_id, key, name, created_at)
    SELECT DISTINCT ON (t.user_id, lower(trim(t.party)))
           t.user_id, left(lower(trim(t.party)), 120), left(trim(t.party), 120), now()
    FROM business_transactions t
    WHERE t.party IS NOT NULL AND trim(t.party) <> ''
    ORDER BY t.user_id, lower(trim(t.party)), t.id DESC
    ON CONFLICT (user_id, key) DO NOTHING
    """,
    # bank names confirmed for someone with no entries left
    """
    INSERT INTO parties (user_id, key, name, created_at)
    SELECT DISTINCT ON (a.user_id, lower(trim(a.party)))
           a.user_id, left(lower(trim(a.party)), 120), left(trim(a.party), 120), now()
    FROM party_aliases a
    WHERE trim(a.party) <> ''
    ORDER BY a.user_id, lower(trim(a.party)), a.id DESC
    ON CONFLICT (user_id, key) DO NOTHING
    """,
    """
    UPDATE business_transactions t SET party_id = p.id
    FROM parties p
    WHERE p.user_id = t.user_id AND p.key = left(lower(trim(t.party)), 120) AND t.party_id IS NULL
    """,
    """
    UPDATE party_aliases a SET party_id = p.id
    FROM parties p
    WHERE p.user_id = a.user_id AND p.key = left(lower(trim(a.party)), 120) AND a.party_id IS NULL
    """,
]


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "parties" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "parties",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(120), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_parties"),
            sa.UniqueConstraint("user_id", "key", name="uq_parties_user_id_key"),
        )
        op.create_index("ix_parties_user_id", "parties", ["user_id"])
    for table in ("business_transactions", "party_aliases"):
        if "party_id" not in _columns(table):
            op.add_column(table, sa.Column("party_id", sa.Integer(), nullable=True))
            op.create_foreign_key(f"fk_{table}_party_id", table, "parties", ["party_id"], ["id"])
            op.create_index(f"ix_{table}_party_id", table, ["party_id"])
    for statement in BACKFILL:
        op.execute(statement)


def downgrade() -> None:
    for table in ("party_aliases", "business_transactions"):
        if "party_id" in _columns(table):
            op.drop_index(f"ix_{table}_party_id", table_name=table)
            op.drop_constraint(f"fk_{table}_party_id", table, type_="foreignkey")
            op.drop_column(table, "party_id")
    if "parties" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_parties_user_id", table_name="parties")
        op.drop_table("parties")

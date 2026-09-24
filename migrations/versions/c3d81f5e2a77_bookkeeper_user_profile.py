"""bookkeeper user profile: business name, phone; drop payment balance

Revision ID: c3d81f5e2a77
Revises: b7c2e19a4f10
Create Date: 2026-09-24

The user table was created by create_tables.py (create_all), not a migration, so each
step checks what exists first. The old payment tables (transactions,
step_up_verifications) are deliberately left in place — drop them by hand once you're
sure nothing in them is needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d81f5e2a77'
down_revision: Union[str, Sequence[str], None] = 'b7c2e19a4f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "user" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("user")}

    if "business_name" not in columns:
        op.add_column("user", sa.Column("business_name", sa.String(100), nullable=True))
    if "phone" not in columns:
        op.add_column("user", sa.Column("phone", sa.String(20), nullable=True))
        op.create_unique_constraint("uq_user_phone", "user", ["phone"])
    if "created_at" not in columns:
        op.add_column("user", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                                        server_default=sa.func.now()))
    if "balance" in columns:
        op.drop_column("user", "balance")
    op.alter_column("user", "email", type_=sa.String(254), existing_nullable=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "user" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("user")}
    if "balance" not in columns:
        op.add_column("user", sa.Column("balance", sa.DECIMAL(10, 2), nullable=False, server_default="0"))
    if "phone" in columns:
        op.drop_constraint("uq_user_phone", "user", type_="unique")
        op.drop_column("user", "phone")
    if "business_name" in columns:
        op.drop_column("user", "business_name")
    if "created_at" in columns:
        op.drop_column("user", "created_at")

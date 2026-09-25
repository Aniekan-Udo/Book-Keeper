"""statement links: private, expiring links to a customer's statement

Revision ID: f1a7c3e9b254
Revises: e8f4b2c6d913
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a7c3e9b254'
down_revision: Union[str, Sequence[str], None] = 'e8f4b2c6d913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "statement_links" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "statement_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("party", sa.String(120), nullable=False),
        sa.Column("party_key", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_statement_links_user_id"),
        sa.PrimaryKeyConstraint("id", name="pk_statement_links"),
        sa.UniqueConstraint("token", name="uq_statement_links_token"),
    )
    op.create_index("ix_statement_links_user_id", "statement_links", ["user_id"])


def downgrade() -> None:
    if "statement_links" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("statement_links")

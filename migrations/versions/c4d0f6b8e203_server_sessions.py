"""server-side sessions replace JWT access/refresh tokens

Revision ID: c4d0f6b8e203
Revises: b3c9e5a7d192
Create Date: 2026-09-25

Everyone is logged out once: existing refresh tokens are dropped, and a new login
creates a session.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d0f6b8e203'
down_revision: Union[str, Sequence[str], None] = 'b3c9e5a7d192'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "sessions" not in tables:
        op.create_table(
            "sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_sessions_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_sessions"),
            sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        )
        op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    if "refresh_tokens" in tables:
        op.drop_table("refresh_tokens")


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "refresh_tokens" not in tables:
        op.create_table(
            "refresh_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("jti", sa.String(50), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        )
        op.create_index("ix_refresh_tokens_id", "refresh_tokens", ["id"])
        op.create_index("ix_refresh_tokens_jti", "refresh_tokens", ["jti"], unique=True)
        op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    if "sessions" in tables:
        op.drop_table("sessions")

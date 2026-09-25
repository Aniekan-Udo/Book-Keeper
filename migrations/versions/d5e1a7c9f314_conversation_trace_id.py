"""conversation turns remember their Opik trace

Revision ID: d5e1a7c9f314
Revises: c4d0f6b8e203
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e1a7c9f314'
down_revision: Union[str, Sequence[str], None] = 'c4d0f6b8e203'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "conversation_turns" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("conversation_turns")}
        if "trace_id" not in columns:
            op.add_column("conversation_turns", sa.Column("trace_id", sa.String(64), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "conversation_turns" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("conversation_turns")}
        if "trace_id" in columns:
            op.drop_column("conversation_turns", "trace_id")

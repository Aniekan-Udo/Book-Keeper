"""conversation memory and WhatsApp inbound message dedupe

Revision ID: d5a90c1b7e34
Revises: c3d81f5e2a77
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a90c1b7e34'
down_revision: Union[str, Sequence[str], None] = 'c3d81f5e2a77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()

    if "conversation_turns" not in tables:
        op.create_table(
            "conversation_turns",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.Enum("user", "assistant", name="conversation_role"), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("channel", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_conversation_turns_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_conversation_turns"),
        )
        op.create_index("ix_conversation_turns_id", "conversation_turns", ["id"])
        op.create_index("ix_conversation_turns_user_id", "conversation_turns", ["user_id"])
        op.create_index("ix_conversation_turns_created_at", "conversation_turns", ["created_at"])

    if "whatsapp_inbound_messages" not in tables:
        op.create_table(
            "whatsapp_inbound_messages",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("wamid", sa.String(255), nullable=False),
            sa.Column("wa_from", sa.String(20), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_whatsapp_inbound_messages"),
            sa.UniqueConstraint("wamid", name="uq_whatsapp_inbound_messages_wamid"),
        )


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "whatsapp_inbound_messages" in tables:
        op.drop_table("whatsapp_inbound_messages")
    if "conversation_turns" in tables:
        op.drop_table("conversation_turns")
        sa.Enum(name="conversation_role").drop(op.get_bind(), checkfirst=True)

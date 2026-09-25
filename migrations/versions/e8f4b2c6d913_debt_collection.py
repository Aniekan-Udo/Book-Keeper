"""debt collection: due dates, contacts, reminders, opt-outs

Revision ID: e8f4b2c6d913
Revises: d5a90c1b7e34
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8f4b2c6d913'
down_revision: Union[str, Sequence[str], None] = 'd5a90c1b7e34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()

    if "business_transactions" in tables:
        columns = {c["name"] for c in inspector.get_columns("business_transactions")}
        if "due_date" not in columns:
            op.add_column("business_transactions", sa.Column("due_date", sa.Date(), nullable=True))

    if "contacts" not in tables:
        op.create_table(
            "contacts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("name_key", sa.String(120), nullable=False),
            sa.Column("phone", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_contacts_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_contacts"),
            sa.UniqueConstraint("user_id", "name_key", name="uq_contacts_user_id_name_key"),
        )
        op.create_index("ix_contacts_id", "contacts", ["id"])
        op.create_index("ix_contacts_user_id", "contacts", ["user_id"])

    if "reminders" not in tables:
        op.create_table(
            "reminders",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("party", sa.String(120), nullable=False),
            sa.Column("phone", sa.String(20), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("details", sa.Text(), nullable=False),
            sa.Column("status", sa.Enum("draft", "sent", "failed", name="reminder_status"), nullable=False),
            sa.Column("wa_message_id", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_reminders_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_reminders"),
        )
        op.create_index("ix_reminders_id", "reminders", ["id"])
        op.create_index("ix_reminders_user_id", "reminders", ["user_id"])
        op.create_index("ix_reminders_phone", "reminders", ["phone"])

    if "reminder_opt_outs" not in tables:
        op.create_table(
            "reminder_opt_outs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("phone", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_reminder_opt_outs"),
            sa.UniqueConstraint("phone", name="uq_reminder_opt_outs_phone"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    for table in ("reminder_opt_outs", "reminders", "contacts"):
        if table in tables:
            op.drop_table(table)
    sa.Enum(name="reminder_status").drop(op.get_bind(), checkfirst=True)
    if "business_transactions" in tables:
        columns = {c["name"] for c in inspector.get_columns("business_transactions")}
        if "due_date" in columns:
            op.drop_column("business_transactions", "due_date")

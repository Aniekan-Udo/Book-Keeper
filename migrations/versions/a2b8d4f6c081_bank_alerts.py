"""bank alerts and party aliases

Revision ID: a2b8d4f6c081
Revises: f1a7c3e9b254
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2b8d4f6c081'
down_revision: Union[str, Sequence[str], None] = 'f1a7c3e9b254'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()

    if "bank_alerts" not in tables:
        op.create_table(
            "bank_alerts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("direction", sa.Enum("in", "out", name="alert_direction"), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("counterparty", sa.String(120), nullable=True),
            sa.Column("narration", sa.String(255), nullable=True),
            sa.Column("occurred_on", sa.Date(), nullable=True),
            sa.Column("reference", sa.String(120), nullable=True),
            sa.Column("status", sa.Enum("pending", "recorded", "already_recorded", "ignored", name="alert_status"),
                      nullable=False),
            sa.Column("transaction_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_bank_alerts_user_id"),
            sa.ForeignKeyConstraint(["transaction_id"], ["business_transactions.id"],
                                    name="fk_bank_alerts_transaction_id"),
            sa.PrimaryKeyConstraint("id", name="pk_bank_alerts"),
            sa.UniqueConstraint("user_id", "fingerprint", name="uq_bank_alerts_user_id_fingerprint"),
        )
        op.create_index("ix_bank_alerts_id", "bank_alerts", ["id"])
        op.create_index("ix_bank_alerts_user_id", "bank_alerts", ["user_id"])

    if "party_aliases" not in tables:
        op.create_table(
            "party_aliases",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("alias_key", sa.String(120), nullable=False),
            sa.Column("party", sa.String(120), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_party_aliases_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_party_aliases"),
            sa.UniqueConstraint("user_id", "alias_key", name="uq_party_aliases_user_id_alias_key"),
        )
        op.create_index("ix_party_aliases_user_id", "party_aliases", ["user_id"])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "party_aliases" in tables:
        op.drop_table("party_aliases")
    if "bank_alerts" in tables:
        op.drop_table("bank_alerts")
    bind = op.get_bind()
    sa.Enum(name="alert_status").drop(bind, checkfirst=True)
    sa.Enum(name="alert_direction").drop(bind, checkfirst=True)

"""ledger integrity: numeric money, soft delete, batches, repayment allocations

Revision ID: b7c2e19a4f10
Revises: 4df41001e977
Create Date: 2026-09-24

business_transactions was originally created by create_tables.py (create_all), not by a
migration, so every step here checks what already exists before changing it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c2e19a4f10'
down_revision: Union[str, Sequence[str], None] = '4df41001e977'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONEY_COLUMNS = ("amount", "amount_paid", "outstanding")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "business_transactions" not in tables:
        # fresh database: create_tables.py / create_all builds the current schema
        return

    columns = {c["name"] for c in inspector.get_columns("business_transactions")}

    for name in MONEY_COLUMNS:
        op.alter_column(
            "business_transactions", name,
            type_=sa.Numeric(18, 2), existing_nullable=True,
            postgresql_using=f"round({name}::numeric, 2)",
        )

    if "batch_id" not in columns:
        op.add_column("business_transactions", sa.Column("batch_id", sa.String(36), nullable=True))
        op.create_index("ix_business_transactions_batch_id", "business_transactions", ["batch_id"])
    if "is_deleted" not in columns:
        op.add_column("business_transactions",
                      sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))

    # loans recorded before this change defaulted amount_paid to the full amount, which made
    # them look already repaid; nothing could have repaid them yet, so reopen them
    op.execute("""
        UPDATE business_transactions
        SET amount_paid = 0, outstanding = amount
        WHERE type IN ('loan_taken', 'loan_given') AND amount IS NOT NULL AND amount_paid = amount
    """)
    # amounts filled in via /clarify never set amount_paid, so those sales were missing from revenue
    op.execute("""
        UPDATE business_transactions
        SET amount_paid = amount, outstanding = 0
        WHERE type IN ('sale', 'expense') AND amount IS NOT NULL AND amount_paid IS NULL
    """)

    if "repayment_allocations" not in tables:
        op.create_table(
            "repayment_allocations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("repayment_id", sa.Integer(), nullable=False),
            sa.Column("applied_to_id", sa.Integer(), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.ForeignKeyConstraint(["repayment_id"], ["business_transactions.id"],
                                    name="fk_repayment_allocations_repayment_id"),
            sa.ForeignKeyConstraint(["applied_to_id"], ["business_transactions.id"],
                                    name="fk_repayment_allocations_applied_to_id"),
            sa.PrimaryKeyConstraint("id", name="pk_repayment_allocations"),
        )
        op.create_index("ix_repayment_allocations_id", "repayment_allocations", ["id"])
        op.create_index("ix_repayment_allocations_repayment_id", "repayment_allocations", ["repayment_id"])
        op.create_index("ix_repayment_allocations_applied_to_id", "repayment_allocations", ["applied_to_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "repayment_allocations" in tables:
        op.drop_table("repayment_allocations")
    if "business_transactions" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("business_transactions")}
    if "is_deleted" in columns:
        op.drop_column("business_transactions", "is_deleted")
    if "batch_id" in columns:
        op.drop_index("ix_business_transactions_batch_id", table_name="business_transactions")
        op.drop_column("business_transactions", "batch_id")
    for name in MONEY_COLUMNS:
        op.alter_column("business_transactions", name, type_=sa.Float(), existing_nullable=True)

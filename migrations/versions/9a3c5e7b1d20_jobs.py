"""jobs: durable background work (graph sync, memory curation)

Revision ID: 9a3c5e7b1d20
Revises: d5e1a7c9f314
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9a3c5e7b1d20'
down_revision: Union[str, Sequence[str], None] = 'd5e1a7c9f314'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "jobs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    # the worker's lookups: a user's oldest pending job, and users with due work
    op.create_index("ix_jobs_status_user_id_id", "jobs", ["status", "user_id", "id"])


def downgrade() -> None:
    if "jobs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_jobs_status_user_id_id", table_name="jobs")
        op.drop_index("ix_jobs_user_id", table_name="jobs")
        op.drop_table("jobs")

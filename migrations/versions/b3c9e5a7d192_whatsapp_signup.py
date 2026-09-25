"""WhatsApp sign-up and phone ownership checks

Revision ID: b3c9e5a7d192
Revises: a2b8d4f6c081
Create Date: 2026-09-25

Security fix: user.phone decides whose books a WhatsApp message goes into, but until now
it was whatever was typed on the web sign-up form, never verified — anyone could claim
someone else's number. From now on it's only set after the owner proves it from WhatsApp.
Existing numbers were never proven, so they are cleared here; those users reconnect with
"Connect WhatsApp" on the website (one message). The cleared numbers are logged by this
migration so they can be told.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c9e5a7d192'
down_revision: Union[str, Sequence[str], None] = 'a2b8d4f6c081'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()

    if "user" in tables:
        unverified = bind.execute(sa.text('SELECT id, email, phone FROM "user" WHERE phone IS NOT NULL')).all()
        for user_id, email, phone in unverified:
            print(f"  clearing unverified phone for user {user_id} ({email}): {phone}")
        op.execute('UPDATE "user" SET phone = NULL')
        op.alter_column("user", "email", existing_type=sa.String(254), nullable=True)
        op.alter_column("user", "password", existing_type=sa.String(255), nullable=True)

    if "onboarding_sessions" not in tables:
        op.create_table(
            "onboarding_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("wa_from", sa.String(20), nullable=False),
            sa.Column("step", sa.Enum("business_name", "name", name="onboarding_step"), nullable=False),
            sa.Column("business_name", sa.String(100), nullable=True),
            sa.Column("first_message", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_onboarding_sessions"),
            sa.UniqueConstraint("wa_from", name="uq_onboarding_sessions_wa_from"),
        )

    if "phone_challenges" not in tables:
        op.create_table(
            "phone_challenges",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("public_id", sa.String(64), nullable=False),
            sa.Column("code", sa.String(6), nullable=False),
            sa.Column("purpose", sa.Enum("login", "link", name="challenge_purpose"), nullable=False),
            sa.Column("expected_phone", sa.String(20), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.Enum("pending", "approved", "consumed", "rejected", name="challenge_status"),
                      nullable=False),
            sa.Column("rejection", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_phone_challenges_user_id"),
            sa.PrimaryKeyConstraint("id", name="pk_phone_challenges"),
            sa.UniqueConstraint("public_id", name="uq_phone_challenges_public_id"),
        )
        op.create_index("ix_phone_challenges_code", "phone_challenges", ["code"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "user" in tables:
        whatsapp_only = bind.execute(
            sa.text('SELECT count(*) FROM "user" WHERE email IS NULL OR password IS NULL')).scalar()
        if whatsapp_only:
            # never delete people's accounts and books to make a downgrade fit
            raise RuntimeError(f"{whatsapp_only} users signed up on WhatsApp and have no email/password; "
                               "the old schema can't hold them. Not downgrading.")
    for table in ("phone_challenges", "onboarding_sessions"):
        if table in tables:
            op.drop_table(table)
    for enum in ("challenge_status", "challenge_purpose", "onboarding_step"):
        sa.Enum(name=enum).drop(bind, checkfirst=True)
    if "user" in tables:
        op.alter_column("user", "email", existing_type=sa.String(254), nullable=False)
        op.alter_column("user", "password", existing_type=sa.String(255), nullable=False)
    # cleared phone numbers can't be restored

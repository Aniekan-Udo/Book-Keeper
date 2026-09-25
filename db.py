import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import MetaData, String, Date, Integer, Text, Float, ForeignKey, DateTime, Boolean, JSON, Numeric, UniqueConstraint, Enum as SQLEnum

from dotenv import load_dotenv
load_dotenv()

POSTGRES_URI = os.getenv(
    "POSTGRES_URI",
    "postgresql+asyncpg://postgres:bookkeeper@postgres:5432/postgres"
)

engine = create_async_engine(url=POSTGRES_URI, pool_size=100, max_overflow=50,
                              pool_timeout=60, pool_pre_ping=True, echo=False)
SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Model(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": 'ix_%(column_0_label)s',
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "pk": "pk_%(table_name)s",
    })


class User(Model):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    firstname: Mapped[str] = mapped_column(String(50), nullable=False)
    lastname: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    business_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # E.164, e.g. +2348012345678 — how the WhatsApp channel will identify the user
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"User(id={self.id}, email={self.email}, business_name={self.business_name})"


class RefreshToken(Model):
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    jti: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class IdempotencyRecord(Model):
    __tablename__ = "idempotency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class TransactionORM(Model):
    __tablename__ = "business_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # every transaction extracted from one message shares a batch_id, so "undo that" can
    # remove the whole message's worth of entries at once
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    type: Mapped[str] = mapped_column(
        SQLEnum("sale", "expense", "loan_taken", "loan_given", "debt_repayment", name="transaction_type"),
        nullable=False,
        index=True,
    )
    party: Mapped[str | None] = mapped_column(String(120), nullable=True)
    item: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # for sales/expenses: how much of `amount` has been settled; for loans: how much has been repaid
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # still owed on this entry — to the user (sale, loan_given) or by the user (expense, loan_taken)
    outstanding: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    direction: Mapped[str] = mapped_column(
        SQLEnum("in", "out", name="transaction_direction"),
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    # when the other party promised to pay ("she go pay Friday"); drives overdue + reminders
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        SQLEnum("complete", "needs_amount", name="transaction_status"),
        nullable=False,
        default="complete",
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class RepaymentAllocation(Model):
    """How much of a debt_repayment went to settle which sale/expense/loan."""
    __tablename__ = "repayment_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repayment_id: Mapped[int] = mapped_column(Integer, ForeignKey("business_transactions.id"), nullable=False, index=True)
    applied_to_id: Mapped[int] = mapped_column(Integer, ForeignKey("business_transactions.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


class ConversationTurn(Model):
    """Recent chat history, so short follow-ups ("15k", "cancel that") make sense.
    Only the user's text and the assistant's final reply are kept, not tool calls."""
    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(SQLEnum("user", "assistant", name="conversation_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # "web" / "whatsapp"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True
    )


class WhatsAppInboundMessage(Model):
    """Every WhatsApp message id we've accepted. Meta redelivers webhooks, and the unique
    constraint is what stops a redelivery from recording the same sale twice."""
    __tablename__ = "whatsapp_inbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wamid: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    wa_from: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Contact(Model):
    """A trader's customer/supplier phone number, so reminders can reach them."""
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "name_key", name="uq_contacts_user_id_name_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # lower-cased, trimmed name — matches how debts are grouped by party
    name_key: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Reminder(Model):
    """A payment reminder to a debtor: drafted, shown to the trader, then sent on confirmation."""
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    party: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        SQLEnum("draft", "sent", "failed", name="reminder_status"), nullable=False, default="draft"
    )
    wa_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReminderOptOut(Model):
    """Numbers that replied STOP. Checked before every reminder, from any trader."""
    __tablename__ = "reminder_opt_outs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class StatementLink(Model):
    """An unguessable, expiring, revocable link to one customer's statement, for the
    trader to share. Renders live data, so it's always current."""
    __tablename__ = "statement_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    party: Mapped[str] = mapped_column(String(120), nullable=False)
    party_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)

async def get_db_session():
    async with SessionLocal() as session:
        yield session

@asynccontextmanager
async def get_db():
    async with SessionLocal() as session:
        yield session


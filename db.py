import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import MetaData, String, Date, Integer, Text, Float, ForeignKey, DateTime, Boolean, JSON, Numeric, UniqueConstraint, Index, Enum as SQLEnum

from dotenv import load_dotenv
load_dotenv()

POSTGRES_URI = os.getenv(
    "POSTGRES_URI",
    "postgresql+asyncpg://postgres:bookkeeper@postgres:5432/postgres"
)

# Connections per process. Keep (processes × (DB_POOL_SIZE + DB_MAX_OVERFLOW)) under the
# server's max_connections (Postgres default: 100), leaving room for migrations and admin.
# One conversation turn uses up to two: its session, and the per-user lock (tools/turn_lock.py).
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))

engine = create_async_engine(url=POSTGRES_URI, pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW,
                              pool_timeout=30, pool_pre_ping=True, echo=False)
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
    # email/password are optional: traders who sign up on WhatsApp have neither
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # E.164, e.g. +2348012345678. Only ever set once ownership is proven — a WhatsApp message
    # from this number — because it decides whose books WhatsApp messages go into.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"User(id={self.id}, email={self.email}, business_name={self.business_name})"


class UserSession(Model):
    """A logged-in browser. The cookie holds a random token; only its SHA-256 is stored,
    so a database leak doesn't hand out working logins. Deleting the row logs out."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class IdempotencyRecord(Model):
    __tablename__ = "idempotency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Party(Model):
    """Someone in a trader's books, with a stable id. Entries, bank names and graph nodes
    point at this id; the name key (lower-cased, trimmed) is how a typed name finds it."""
    __tablename__ = "parties"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_parties_user_id_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # no foreign key, like business_transactions.user_id: a party exists because entries do
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)  # the latest spelling
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


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
    # who `party` is: set whenever party is (tools/parties.py)
    party_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("parties.id"), nullable=True, index=True)
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
    # on assistant turns: the Opik trace showing how the reply was produced (if Opik is on)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class BankAlert(Model):
    """A bank/POS alert the user forwarded. Parsed fields only — the raw text (with
    balances and account numbers) isn't kept. Never a connection to the bank."""
    __tablename__ = "bank_alerts"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint", name="uq_bank_alerts_user_id_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    # identifies the same alert forwarded twice
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)       # credit_alert / debit_alert / sender_receipt / pos_receipt
    direction: Mapped[str] = mapped_column(SQLEnum("in", "out", name="alert_direction"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(120), nullable=True)
    narration: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(
        SQLEnum("pending", "recorded", "already_recorded", "ignored", name="alert_status"), nullable=False
    )
    transaction_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("business_transactions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PartyAlias(Model):
    """How a customer's name appears on bank alerts ("MAMA CHIDI OKAFOR") → who it is,
    learned when the trader confirms who sent money. The one store of bank names: matching
    alerts reads only this (it works with graph memory off); the graph mirrors it."""
    __tablename__ = "party_aliases"
    __table_args__ = (UniqueConstraint("user_id", "alias_key", name="uq_party_aliases_user_id_alias_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    alias_key: Mapped[str] = mapped_column(String(120), nullable=False)
    party: Mapped[str] = mapped_column(String(120), nullable=False)
    party_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("parties.id"), nullable=True, index=True)


class OnboardingSession(Model):
    """A stranger signing up over WhatsApp, between their first message and account creation."""
    __tablename__ = "onboarding_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_from: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    step: Mapped[str] = mapped_column(SQLEnum("business_name", "name", name="onboarding_step"), nullable=False)
    business_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # their first message, replayed once the account exists if it was a transaction
    first_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PhoneChallenge(Model):
    """Proof of phone ownership, the other way round from SMS codes: the website shows a
    code and the user sends it to our WhatsApp number from their phone. Used to log in
    without a password and to connect a number to a web account."""
    __tablename__ = "phone_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # what the browser polls with; secret, unlike the short code
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(SQLEnum("login", "link", name="challenge_purpose"), nullable=False)
    # only a message from this number can complete the challenge
    expected_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    # link: the web account asking to add the phone; login: filled in on approval
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user.id"), nullable=True)
    status: Mapped[str] = mapped_column(
        SQLEnum("pending", "approved", "consumed", "rejected", name="challenge_status"), nullable=False
    )
    rejection: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Job(Model):
    """Durable background work: graph sync after a ledger change, and memory curation after
    a reply (jobs/queue.py). Written in the same transaction as the change that causes it,
    so a crash can't lose it. Done jobs are deleted; failed ones stay for inspection."""
    __tablename__ = "jobs"
    # the worker's lookups: a user's oldest pending job, and users with due work
    __table_args__ = (Index("ix_jobs_status_user_id_id", "status", "user_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)        # ledger_changed / curate
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")  # pending / failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


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


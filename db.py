import os
import enum
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from sqlalchemy import Date

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import MetaData, String, Date, Integer, Text, Float, ForeignKey, DECIMAL, DateTime, Boolean, JSON, Enum as SQLEnum

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
    email: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    balance: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=False, default=0.0)

    def __repr__(self):
        return f"User(id={self.id}, first_name={self.firstname}, last_name={self.lastname}, email={self.email}, balance={self.balance})"


class RefreshToken(Model):
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    jti: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StepUpVerification(Model):
    __tablename__ = "step_up_verifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(20), nullable=False)  # "password", "biometric", "otp"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyRecord(Model):
    __tablename__ = "idempotency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Business(Model):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    industry: Mapped[str] = mapped_column(String(50), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)


class TransactionStatus(str, enum.Enum):
    completed = "completed"
    failed = "failed"
    reversed = "reversed"


class Transaction(Model):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    reference: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    recipient_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(DECIMAL(18, 2), nullable=False)
    sender_balance_after: Mapped[float] = mapped_column(DECIMAL(18, 2), nullable=False)
    recipient_balance_after: Mapped[float] = mapped_column(DECIMAL(18, 2), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(SQLEnum(TransactionStatus), nullable=False, default=TransactionStatus.completed)
    idempotency_key: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

class TransactionORM(Model):
    __tablename__ = "business_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    type: Mapped[str] = mapped_column(
        SQLEnum("sale", "expense", "loan_taken", "loan_given", "debt_repayment", name="transaction_type"),
        nullable=False,
        index=True,
    )
    party: Mapped[str | None] = mapped_column(String(120), nullable=True)
    item: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    outstanding: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str] = mapped_column(
        SQLEnum("in", "out", name="transaction_direction"),
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        SQLEnum("complete", "needs_amount", name="transaction_status"),
        nullable=False,
        default="complete",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Expense(Model):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"), nullable=False)
    amount: Mapped[DECIMAL] = mapped_column(DECIMAL(18, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Sale(Model):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"), nullable=False)
    amount: Mapped[DECIMAL] = mapped_column(DECIMAL(18, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    customer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "cash", "transfer", "pos"
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=...)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # soft delete


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


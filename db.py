import os
import enum
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import MetaData, String, Integer, DECIMAL, DateTime, Boolean, JSON, Enum as SQLEnum

LOCAL_POSTGRES_URI = os.getenv(
    "POSTGRES_URI",
    "postgresql+asyncpg://test:test@localhost:5434/test"
)

engine = create_async_engine(url=LOCAL_POSTGRES_URI, pool_size=100, max_overflow=50,
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


async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)


@asynccontextmanager
async def get_db():
    async with SessionLocal() as session:
        yield session
"""
Shared fixtures for the auth/payment test suite.

ADAPT THESE IMPORTS to match your actual project layout if they differ:
    from db import Model, User, RefreshToken, StepUpVerification, Transaction, IdempotencyRecord, get_db
    from authentication.auth import hash_password

Uses a separate test database so tests never touch dev/prod data.
Set TEST_DATABASE_URL in your environment, or it defaults to a local
Postgres test DB on a different port/db name than your dev DB.
"""
import os
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from db import Model, User
from authentication.auth import hash_password
from main import app

app.state.limiter.enabled = False

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5434/test_db",
)

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create all tables once per test session, drop them at the end."""
    async def init_db():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Model.metadata.create_all)
        await engine.dispose()

    async def drop_db():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Model.metadata.drop_all)
        await engine.dispose()

    asyncio.run(init_db())
    yield
    asyncio.run(drop_db())


@pytest_asyncio.fixture
async def db_session(setup_test_db):
    """
    A DB session wrapped in a transaction that's rolled back after each
    test. This keeps tests isolated from each other without needing to
    manually delete rows every time.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    TestSessionLocal = async_sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, join_transaction_mode="create_savepoint")
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = TestSessionLocal(bind=conn)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def test_user(db_session):
    """A persisted user with a known password, for login/auth tests."""
    user = User(
        firstname="Test",
        lastname="User",
        email="testuser@example.com",
        password=hash_password("correct-horse-battery-staple"),
        balance=1000,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_recipient(db_session):
    """A second user, for transfer tests."""
    user = User(
        firstname="Recipient",
        lastname="User",
        email="recipient@example.com",
        password=hash_password("some-other-password"),
        balance=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user
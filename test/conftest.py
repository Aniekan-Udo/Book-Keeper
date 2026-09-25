"""
Shared fixtures for the test suite.

Defaults to a throwaway SQLite file so `uv run pytest` works with no services running.
Set TEST_DATABASE_URL to run against Postgres instead, e.g.
    postgresql+asyncpg://test:test@localhost:5434/test_db
"""
import os
import asyncio
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from db import Model, User
from authentication.auth import hash_password
from main import app

app.state.limiter.enabled = False

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "sqlite+aiosqlite:///" + os.path.join(tempfile.gettempdir(), "bookkeeper_test.sqlite3").replace("\\", "/"),
)

def create_async_engine(url, **kwargs):
    engine = _create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # pysqlite's own transaction handling breaks SAVEPOINTs, so the per-test rollback
        # wouldn't undo committed rows. Take over BEGIN ourselves (SQLAlchemy's documented recipe).
        @event.listens_for(engine.sync_engine, "connect")
        def _no_implicit_transactions(dbapi_connection, _):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _explicit_begin(conn):
            conn.exec_driver_sql("BEGIN")
    return engine


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create all tables once per test session, drop them at the end."""
    async def init_db():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Model.metadata.drop_all)  # clear leftovers from an interrupted run
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
async def prod_session(setup_test_db):
    """Like db_session, but with production's expire_on_commit=True. Reading an attribute
    after a commit then fails the way it would in production (an async lazy-load), which
    db_session hides. Use it to exercise code paths that commit."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=True,
                                 join_transaction_mode="create_savepoint")
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = Session(bind=conn)
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
        business_name="Test Stores",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_user(db_session):
    """A second user, for isolation tests."""
    user = User(
        firstname="Other",
        lastname="User",
        email="other@example.com",
        password=hash_password("some-other-password"),
        business_name="Other Stores",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user

@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail any test that would make a real HTTP call (e.g. to Meta) instead of using a
    stub or an explicit transport such as httpx.MockTransport / ASGITransport."""
    import httpx

    class GuardedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            if "transport" not in kwargs:
                raise RuntimeError("Test tried to make a real network call; stub it or pass a transport.")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", GuardedAsyncClient)


@pytest_asyncio.fixture
async def new_browser(db_session):
    """Factory for separate browsers (each with its own cookie jar, i.e. its own login)
    talking to the app on the test database: `ada = await new_browser()`."""
    from httpx import AsyncClient, ASGITransport
    from main import app as fastapi_app
    from db import get_db_session

    async def override_get_db():
        yield db_session

    fastapi_app.dependency_overrides[get_db_session] = override_get_db
    clients = []

    async def make():
        client = AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def no_opik(monkeypatch):
    """Tests never send traces to Opik, even if .env configures it."""
    for name in ("OPIK_API_KEY", "OPIK_URL_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)

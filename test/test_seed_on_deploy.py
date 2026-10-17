"""SEED_DEMO on deploy: `seed_demo --if-missing` seeds once and leaves a seeded database alone."""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete

from db import User
from endpoints.demo import DAY1, DAY30
from scripts import seed_demo


class FakeMemory:
    enabled = True

    async def ping(self):
        return True

    async def close(self):
        pass


class FakeEngine:
    async def dispose(self):
        pass


@pytest.fixture
def seeding(db_session, monkeypatch):
    calls = {"made": [], "deleted": []}

    @asynccontextmanager
    async def get_db():
        yield db_session

    async def make_user(email, business, password):
        db_session.add(User(firstname="Ngozi", lastname="Demo", email=email, password="x"))
        await db_session.flush()
        calls["made"].append(email)
        return len(calls["made"])

    async def delete_demo_user(email, memory):
        await db_session.execute(delete(User).where(User.email == email))
        calls["deleted"].append(email)

    async def nothing(*args, **kwargs):
        pass

    monkeypatch.setattr(seed_demo, "build_graph_memory", FakeMemory)
    monkeypatch.setattr(seed_demo, "get_db", get_db)
    monkeypatch.setattr(seed_demo, "make_user", make_user)
    monkeypatch.setattr(seed_demo, "delete_demo_user", delete_demo_user)
    monkeypatch.setattr(seed_demo, "replay", nothing)
    monkeypatch.setattr(seed_demo, "init_models", nothing)
    monkeypatch.setattr(seed_demo, "engine", FakeEngine())
    return calls


async def test_an_empty_database_is_seeded_and_a_seeded_one_is_left_alone(seeding):
    assert await seed_demo.main(reset=False, if_missing=True) == 0
    assert seeding["made"] == [DAY30[0], DAY1[0]]

    assert await seed_demo.main(reset=False, if_missing=True) == 0
    assert seeding["made"] == [DAY30[0], DAY1[0]]  # nothing new
    assert seeding["deleted"] == []


async def test_a_seed_cut_off_halfway_is_started_over(seeding, db_session):
    db_session.add(User(firstname="Ngozi", lastname="Demo", email=DAY30[0], password="x"))
    await db_session.flush()
    assert await seed_demo.main(reset=False, if_missing=True) == 0
    assert seeding["deleted"] == [DAY30[0]]
    assert DAY1[0] in seeding["made"]

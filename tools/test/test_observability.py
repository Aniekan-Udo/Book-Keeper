"""Opik wiring: a tracer per trader message when configured, passed into the agent run,
and its trace id kept with the reply so the pilot report can point at it.

What the real Opik SDK sends (one trace per message, grouped per trader; every model call
with its prompt; every tool call with arguments and result) was checked against a local
stand-in Opik server when this was built — see the commit that added observability.py.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import observability
from db import ConversationTurn, Model, User
from tools import assistant, router
from tools.pilot_report import build_report


@pytest.fixture(autouse=True)
def opik_off(monkeypatch):
    for name in ("OPIK_API_KEY", "OPIK_URL_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        session.add(User(id=1, firstname="Ada", lastname="", business_name="Ada Provisions"))
        await session.commit()
        yield session
    await engine.dispose()


class FakeTracer:
    def __init__(self, trace_id="trace-123"):
        self.trace_id = trace_id

    def created_traces(self):
        return [SimpleNamespace(id=self.trace_id)]


def test_off_unless_configured():
    assert observability.opik_enabled() is False
    assert observability.message_tracer(1, "whatsapp") is None
    assert observability.trace_id_of(None) is None


def test_configured_tracer_groups_a_traders_messages(monkeypatch):
    monkeypatch.setenv("OPIK_URL_OVERRIDE", "http://127.0.0.1:9/api")  # never contacted here
    tracer = observability.message_tracer(7, "whatsapp")
    assert type(tracer).__name__ == "OpikTracer"


async def test_reply_keeps_its_trace_id(db, monkeypatch):
    seen = {}

    async def handle_message(text, prompt, tools, history=None, callbacks=None):
        seen["callbacks"] = callbacks
        return {"messages": [AIMessage(content="Recorded.")]}

    tracer = FakeTracer("trace-abc")
    monkeypatch.setattr(assistant, "message_tracer", lambda user_id, channel: tracer)
    monkeypatch.setattr(router, "handle_message", handle_message)

    await assistant.respond(db, 1, "I sold rice 5k", channel="whatsapp")

    assert seen["callbacks"] == [tracer]
    turns = (await db.execute(select(ConversationTurn).order_by(ConversationTurn.id))).scalars().all()
    assert [(t.role, t.trace_id) for t in turns] == [("user", None), ("assistant", "trace-abc")]


async def test_no_tracer_no_callbacks(db, monkeypatch):
    seen = {}

    async def handle_message(text, prompt, tools, history=None, callbacks=None):
        seen["callbacks"] = callbacks
        return {"messages": [AIMessage(content="Recorded.")]}

    monkeypatch.setattr(router, "handle_message", handle_message)
    await assistant.respond(db, 1, "I sold rice 5k", channel="web")
    assert seen["callbacks"] is None


async def test_router_hands_callbacks_to_the_agent_run(monkeypatch):
    seen = {}

    class Agent:
        async def ainvoke(self, payload, config=None):
            seen["config"] = config
            return {"messages": [AIMessage(content="ok")]}

    monkeypatch.setattr(router, "create_agent", lambda **_: Agent())
    monkeypatch.setattr(router, "get_chat_model", lambda: None)
    tracer = FakeTracer()
    await router.handle_message("hi", "prompt", [], callbacks=[tracer])
    assert seen["config"] == {"callbacks": [tracer], "run_name": "trader message"}


async def test_pilot_report_points_at_the_trace(db):
    now = datetime.now(timezone.utc)
    db.add_all([
        ConversationTurn(user_id=1, role="user", content="Paid 50k fuel", channel="whatsapp", created_at=now),
        ConversationTurn(user_id=1, role="assistant", content="Recorded ₦50,000.", channel="whatsapp",
                         created_at=now, trace_id="trace-xyz"),
        ConversationTurn(user_id=1, role="user", content="no be 50k, na 45k", channel="whatsapp", created_at=now),
    ])
    await db.commit()
    report = await build_report(db, days=7)
    assert "Opik trace: `trace-xyz`" in report

"""The agent runner's safety nets: a ceiling on each run, and one retry when the model
writes a tool call the provider can't parse."""
import asyncio

import pytest
from langchain_core.messages import AIMessage

from tools import router


@pytest.fixture(autouse=True)
def fresh_breaker():
    router.llm_breaker.record_success()
    yield
    router.llm_breaker.record_success()


def agent_that(*behaviours):
    """create_agent stand-in: each run does the next behaviour (an exception to raise,
    a coroutine function to await, or a reply)."""
    runs = []

    class Agent:
        async def ainvoke(self, payload, config=None):
            b = behaviours[len(runs)]
            runs.append(b)
            if isinstance(b, Exception):
                raise b
            if callable(b):
                return await b()
            return {"messages": [AIMessage(content=b)]}

    return Agent(), runs


SLIP = RuntimeError("Error code: 400 - {'error': {'code': 'tool_use_failed', 'message': 'Failed to parse tool call'}}")


async def test_a_model_slip_is_retried_once(monkeypatch):
    agent, runs = agent_that(SLIP, "Recorded.")
    monkeypatch.setattr(router, "create_agent", lambda **_: agent)
    monkeypatch.setattr(router, "get_chat_model", lambda: None)
    result = await router.handle_message("I sold rice", "prompt", [])
    assert len(runs) == 2 and result["messages"][-1].text == "Recorded."
    assert router.llm_breaker.failures == 0  # not an outage


async def test_a_second_slip_asks_the_trader_to_rephrase(monkeypatch):
    agent, _ = agent_that(SLIP, SLIP)
    monkeypatch.setattr(router, "create_agent", lambda **_: agent)
    monkeypatch.setattr(router, "get_chat_model", lambda: None)
    with pytest.raises(router.LLMUnavailable, match="another way"):
        await router.handle_message("I sold rice", "prompt", [])
    assert router.llm_breaker.failures == 0


async def test_a_stalled_run_gives_up_after_the_turn_timeout(monkeypatch):
    async def stall():
        await asyncio.sleep(10)

    agent, _ = agent_that(stall)
    monkeypatch.setattr(router, "create_agent", lambda **_: agent)
    monkeypatch.setattr(router, "get_chat_model", lambda: None)
    monkeypatch.setattr(router, "TURN_TIMEOUT", 0.05)
    with pytest.raises(router.LLMUnavailable, match="busy"):
        await router.handle_message("I sold rice", "prompt", [])
    assert router.llm_breaker.failures == 1

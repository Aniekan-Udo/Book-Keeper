import asyncio
import os
import time

from langchain.agents import create_agent

from llm import get_chat_model
from utils import logger, provider_problem


class LLMUnavailable(Exception):
    pass


class CircuitBreaker:
    """Stop calling the provider for `reset_timeout` seconds after `fail_max` consecutive
    failures, so an outage fails fast instead of every user waiting on timeouts.
    (pybreaker's decorator can't see exceptions raised inside an awaited coroutine.)"""

    def __init__(self, fail_max: int = 3, reset_timeout: float = 120):
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_timeout:
            # half-open: let the next call through as a probe
            self.opened_at = None
            self.failures = self.fail_max - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.fail_max:
            self.opened_at = time.monotonic()


llm_breaker = CircuitBreaker()

# The longest a trader waits for one agent run (all its model and tool calls) before being
# told to try again. Without it, a provider that stalls (or keeps a request open) holds the
# message, and the trader's per-user lock, for as long as it likes.
TURN_TIMEOUT = float(os.getenv("LLM_TURN_TIMEOUT", "90"))


def _model_slip(e: Exception) -> bool:
    """The model wrote a tool call the provider couldn't parse (Groq: tool_use_failed). A
    glitch in one generation, not an outage: worth one retry, and not a breaker failure."""
    return "tool_use_failed" in str(e)


async def handle_message(user_message: str, system_prompt: str, tools: list,
                         history: list[dict] | None = None, callbacks: list | None = None) -> dict:
    """`history`: earlier turns as [{"role": "user"|"assistant", "content": str}], oldest first.
    `callbacks`: LangChain callbacks for this run, e.g. the Opik tracer — they see every
    model call and tool call inside it."""
    if llm_breaker.is_open:
        logger.warning("LLM circuit breaker is open — rejecting call without hitting the provider")
        raise LLMUnavailable("The assistant is temporarily unavailable. Please try again in a couple of minutes.")

    agent = create_agent(model=get_chat_model(), tools=tools, system_prompt=system_prompt)
    messages = [*(history or []), {"role": "user", "content": user_message}]
    # run_name becomes the trace name in Opik ("trader message" rather than "LangGraph")
    config = {"callbacks": callbacks, "run_name": "trader message"} if callbacks else None
    try:
        try:
            result = await asyncio.wait_for(agent.ainvoke({"messages": messages}, config=config), TURN_TIMEOUT)
        except Exception as e:
            if not _model_slip(e):
                raise
            logger.warning("router.model_slip_retry", error=str(e)[:200])
            result = await asyncio.wait_for(agent.ainvoke({"messages": messages}, config=config), TURN_TIMEOUT)
    except Exception as e:
        if _model_slip(e):
            logger.error("router.model_slip_again", error=str(e)[:300])
            raise LLMUnavailable("I couldn't process that one. Could you say it another way?") from e
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            llm_breaker.record_failure()
            logger.warning("router.turn_timeout", seconds=TURN_TIMEOUT)
            raise LLMUnavailable("The assistant is busy right now. Please try again shortly.") from e
        llm_breaker.record_failure()
        problem = provider_problem(e)
        if problem == "out_of_credit":
            # the operator must act: traders just see "busy"
            logger.error("LLM provider refused the call: account credit is used up. Top up billing now.")
        elif problem:
            logger.warning(f"LLM provider unavailable: {problem}")
        else:
            logger.error(f"Agent invocation failed: {e}", exc_info=True)
            raise
        raise LLMUnavailable("The assistant is busy right now. Please try again shortly.") from e

    llm_breaker.record_success()
    return result

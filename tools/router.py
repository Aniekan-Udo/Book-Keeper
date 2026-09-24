import time

from langchain.agents import create_agent

from llm import get_chat_model
from utils import logger, is_rate_limit_error


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


async def handle_message(user_message: str, system_prompt: str, tools: list,
                         history: list[dict] | None = None) -> dict:
    """`history`: earlier turns as [{"role": "user"|"assistant", "content": str}], oldest first."""
    if llm_breaker.is_open:
        logger.warning("LLM circuit breaker is open — rejecting call without hitting the provider")
        raise LLMUnavailable("The assistant is temporarily unavailable. Please try again in a couple of minutes.")

    agent = create_agent(model=get_chat_model(), tools=tools, system_prompt=system_prompt)
    try:
        messages = [*(history or []), {"role": "user", "content": user_message}]
        result = await agent.ainvoke({"messages": messages})
    except Exception as e:
        llm_breaker.record_failure()
        if is_rate_limit_error(e):
            logger.warning("LLM provider rate-limited the request")
            raise LLMUnavailable("The assistant is busy right now. Please try again shortly.") from e
        logger.error(f"Agent invocation failed: {e}", exc_info=True)
        raise

    llm_breaker.record_success()
    return result

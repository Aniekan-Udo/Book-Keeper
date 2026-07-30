from db import get_db
from langgraph.prebuilt import create_react_agent
from tools.clean_version import build_tools
from llm import llm, State, router_sys_msg
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import List, Optional, Literal, TypedDict
from tools.generate_PnL import generate_pnl
from datetime import datetime, date
from monitoring import tracer
from utils import logger
import pybreaker
from fastapi import HTTPException

class ReportRequest(BaseModel):
    report_type: Literal["pnl", "expenses", "receivables"]
    start_date: Optional[date] = None
    end_date: Optional[date] = None



# Opens after 3 consecutive failures, stays open (rejecting calls immediately) for 120s
llm_breaker = pybreaker.CircuitBreaker(
    fail_max=3,
    reset_timeout=120,
    exclude=[],  # see note below on excluding non-retryable-worthy exceptions
)


class LLMUnavailable(Exception):
    """Raised when the circuit breaker is open — LLM calls are being skipped."""
    pass


@llm_breaker
async def _call_agent(agent, user_message: str) -> dict:
    return await agent.ainvoke({"messages": [{"role": "user", "content": user_message}]})



async def handle_message(user_message, db, system_prompt, model, tools) -> dict:
    agent = create_react_agent(model=model, tools=tools, prompt=system_prompt)
    try:
        return await _call_agent(agent, user_message)
    except pybreaker.CircuitBreakerError:
        logger.warning("LLM circuit breaker is open — rejecting call without hitting the provider")
        raise LLMUnavailable("The assistant is temporarily unavailable. Please try again in a couple of minutes.")
    except Exception as e:
        logger.error(f"Agent invocation failed: {e}", exc_info=True)
        raise
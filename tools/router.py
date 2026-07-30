from db import get_db
from langgraph.prebuilt import create_react_agent
from tools.clean_version import build_tools
from llm import State, GEMINI_KEYS, get_gemini_model  # get_gemini_model + GEMINI_KEYS from earlier
from tools.prompts import router_sys_msg
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import List, Optional, Literal, TypedDict
from tools.generate_PnL import generate_pnl
from datetime import datetime, date
from monitoring import tracer
from utils import logger, is_rate_limit_error
import pybreaker
from fastapi import HTTPException


class ReportRequest(BaseModel):
    report_type: Literal["pnl", "expenses", "receivables"]
    start_date: Optional[date] = None
    end_date: Optional[date] = None


llm_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=120, exclude=[])


class LLMUnavailable(Exception):
    pass


@llm_breaker
async def _call_agent(agent, user_message: str) -> dict:
    return await agent.ainvoke({"messages": [{"role": "user", "content": user_message}]})


async def handle_message(user_message, db, system_prompt, tools) -> dict:
    """Note: `model` param is dropped — model is now built per-attempt inside this function."""
    last_error = None
    for _ in range(len(GEMINI_KEYS)):
        model = get_gemini_model()  # fresh client, next key in rotation
        agent = create_react_agent(model=model, tools=tools, prompt=system_prompt)
        try:
            return await _call_agent(agent, user_message)
        except pybreaker.CircuitBreakerError:
            logger.warning("LLM circuit breaker is open — rejecting call without hitting the provider")
            raise LLMUnavailable("The assistant is temporarily unavailable. Please try again in a couple of minutes.")
        except Exception as e:
            if is_rate_limit_error(e):
                logger.warning("Gemini key rate-limited, rotating to next key")
                last_error = e
                continue
            logger.error(f"Agent invocation failed: {e}", exc_info=True)
            raise

    logger.error(f"All Gemini keys exhausted: {last_error}")
    raise LLMUnavailable("The assistant is temporarily unavailable. Please try again shortly.")
from fastapi import FastAPI, Header, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from tools.router import handle_message
from tools.formatting import resolve_clarification
from db import get_db_session
from llm import llm, router_sys_msg
from tools.clean_version import build_tools
from utils import logger
from monitoring import tracer
from tools.router import LLMUnavailable

from fastapi import FastAPI, Depends, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, Field

from utils import get_cached_response, store_response

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Book-Keeper")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)



class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    user_id: int

class MessageResponse(BaseModel):
    reply: str

class ResolveClarificationRequest(BaseModel):
    transaction_id: int
    amount: float


@app.post("/api/v1/message", response_model=MessageResponse)
@limiter.limit("10/minute")
async def post_message(request: Request, req: MessageRequest, db: AsyncSession = Depends(get_db_session),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),):
    if idempotency_key:
        cached = await get_cached_response(db, idempotency_key, req.user_id)
        if cached:
            return MessageResponse(**cached)

    try:
        tools = build_tools(db, req.user_id)
        sys_msg = router_sys_msg()
        result = await handle_message(req.message, db, sys_msg, llm, tools)
        reply = result["messages"][-1].content or "I wasn't able to process that. Could you rephrase or try again?"
        response = MessageResponse(reply=reply)

        if idempotency_key:
            await store_response(db, idempotency_key, req.user_id, response.model_dump())

        return response
    except LLMUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"post_message failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Something went wrong. Please try again.")


@app.post("/api/v1/clarify", response_model=MessageResponse)
async def post_clarify(req: ResolveClarificationRequest, db: AsyncSession = Depends(get_db_session)):
    try:
        txn = await resolve_clarification(db, req.transaction_id, req.amount)
        return MessageResponse(reply=f"Got it — amount for {txn.item or txn.type} set to ₦{txn.amount:,.2f}.")
    except Exception as e:
        logger.error(f"post_clarify failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Something went wrong processing your message. Please try again.")


# Serve the test client frontend
# Mount at /static so API routes take priority, then serve index at /
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))

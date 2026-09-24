import os

from fastapi import FastAPI, Header, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import get_current_user
from authentication.settings import settings
from channels import whatsapp
from db import User, get_db_session
from endpoints import login, logout, refresh, registration
from tools.assistant import respond
from tools.ledger import LedgerError, resolve_clarification
from tools.router import LLMUnavailable
from utils import get_cached_response, limiter, logger, store_response

app = FastAPI(title="AI Book-Keeper")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,  # the refresh token travels as a cookie
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

for module in (registration, login, refresh, logout, whatsapp):
    app.include_router(module.router)


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)

class MessageResponse(BaseModel):
    reply: str
    clarifications: list[dict] = []

class ResolveClarificationRequest(BaseModel):
    transaction_id: int
    amount: float = Field(..., gt=0)


@app.get("/api/v1/me")
async def me(user: User = Depends(get_current_user)):
    return {"firstname": user.firstname, "lastname": user.lastname, "email": user.email,
            "business_name": user.business_name, "phone": user.phone}


@app.post("/api/v1/message", response_model=MessageResponse)
@limiter.limit("10/minute")
async def post_message(request: Request, req: MessageRequest,
                       db: AsyncSession = Depends(get_db_session),
                       user: User = Depends(get_current_user),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=50)):
    user_id = user.id  # read now: the session commits below and expires ORM attributes
    if idempotency_key:
        cached = await get_cached_response(db, idempotency_key, user_id)
        if cached:
            return MessageResponse(**cached)

    try:
        answer = await respond(db, user_id, req.message, channel="web")
        response = MessageResponse(reply=answer.reply, clarifications=answer.clarifications)

        if idempotency_key:
            await store_response(db, idempotency_key, user_id, response.model_dump())

        return response
    except LLMUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"post_message failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Something went wrong. Please try again.")


@app.post("/api/v1/clarify", response_model=MessageResponse)
async def post_clarify(req: ResolveClarificationRequest,
                       db: AsyncSession = Depends(get_db_session),
                       user: User = Depends(get_current_user)):
    try:
        result = await resolve_clarification(db, user.id, req.transaction_id, req.amount)
        txn = result["updated"]
        reply = f"Got it — amount for {txn['item'] or txn['type'].replace('_', ' ')} set to ₦{txn['amount']:,.2f}."
        if result["notes"]:
            reply += " " + " ".join(result["notes"])
        return MessageResponse(reply=reply)
    except LedgerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"post_clarify failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Something went wrong processing your message. Please try again.")


# Serve the web client. Mounted at /static so API routes take priority, index served at /
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from authentication.auth import get_current_user
from authentication.settings import settings
from channels import whatsapp
from db import User, get_db, get_db_session
from endpoints import login, logout, registration, statements, whatsapp_auth
from llm import get_curator_model
from memory import build_graph_memory, get_graph_memory, set_curator, set_graph_memory
from memory.curator import Curator
from memory.sync import GraphSync
from observability import message_tracer
from tools.assistant import respond
from tools.ledger_events import ledger_events
from tools.ledger import LedgerError, resolve_clarification
from tools.router import LLMUnavailable
from utils import get_cached_response, limiter, logger, store_response

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start graph memory. If FalkorDB is unreachable the app still starts: the ledger
    never depends on the graph; memory features catch up via scripts/rebuild_graph.py."""
    memory = build_graph_memory()
    set_graph_memory(memory)
    graph_sync = GraphSync(memory, get_db)
    curator = Curator(memory, get_db, model_factory=get_curator_model, tracer_factory=message_tracer)
    set_curator(curator)
    if memory.enabled:
        if await memory.ping():
            logger.info("graph_memory.ready")
        else:
            logger.error("graph_memory.unreachable_at_startup")
        ledger_events.subscribe(graph_sync.on_ledger_changed)
    try:
        yield
    finally:
        ledger_events.unsubscribe(graph_sync.on_ledger_changed)
        # let in-flight curation and syncs finish before closing the store
        await curator.drain()
        await ledger_events.drain()
        await memory.close()


app = FastAPI(title="AI Book-Keeper", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,  # the session travels as a cookie
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

for module in (registration, login, logout, whatsapp, statements, whatsapp_auth):
    app.include_router(module.router)


# ── CSRF ─────────────────────────────────────────────────────────────────────
# Logins are cookies, which browsers attach even to requests another site triggers.
# SameSite=Lax already stops most of that; this also refuses any state-changing request
# whose Origin (or Referer) is a different site. Browsers always send Origin on cross-site
# POSTs, so a missing one means a non-browser client (curl, tests), which is allowed.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PREFIXES = ("/webhooks/",)  # server-to-server from Meta, verified by signature


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is None and request.headers.get("referer"):
        parts = urlsplit(request.headers["referer"])
        origin = f"{parts.scheme}://{parts.netloc}"
    return origin


def is_same_site(request: Request) -> bool:
    origin = _request_origin(request)
    if origin is None:
        return True
    if origin == "null":  # sandboxed frames, some redirects — never our own page
        return False
    # compare hosts, not schemes: behind a TLS proxy the app itself sees plain http
    return urlsplit(origin).netloc == request.headers.get("host") or origin in settings.allowed_origins


@app.middleware("http")
async def refuse_cross_site_writes(request: Request, call_next):
    if (request.method in UNSAFE_METHODS and not request.url.path.startswith(CSRF_EXEMPT_PREFIXES)
            and not is_same_site(request)):
        return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)

class MessageResponse(BaseModel):
    reply: str
    clarifications: list[dict] = []

class ResolveClarificationRequest(BaseModel):
    transaction_id: int
    amount: float = Field(..., gt=0)


@app.get("/health", include_in_schema=False)
async def health(db: AsyncSession = Depends(get_db_session)):
    """For uptime monitors: the app is serving and the database answers. The graph is
    reported but doesn't fail the check: bookkeeping works without it."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "database unavailable"}, status_code=503)
    memory = get_graph_memory()
    graph = "off" if not memory.enabled else ("ok" if await memory.ping() else "unavailable")
    return {"status": "ok", "graph": graph}


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

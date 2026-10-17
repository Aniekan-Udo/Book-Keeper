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
from db import User, engine, get_db, get_db_session
from endpoints import dashboard, demo, login, logout, registration, statements, whatsapp_auth
from llm import get_curator_model, warm_up as warm_up_models
from memory import build_graph_memory, get_graph_memory, set_curator, set_graph_memory
from memory.curator import Curator
from memory.models import GraphMemoryError
from memory.sync import GraphSync
from jobs import queue as job_queue
from jobs.queue import JobQueue
from jobs.worker import JobWorker
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
    warm_up_models()  # before serving: otherwise the first message pays for importing the model client
    memory = build_graph_memory()
    set_graph_memory(memory)
    graph_sync = GraphSync(memory, get_db)
    curator = Curator(memory, get_db, model_factory=get_curator_model, tracer_factory=message_tracer)
    set_curator(curator)
    worker = None
    if memory.enabled:
        if await memory.ping():
            logger.info("graph_memory.ready")
        else:
            logger.error("graph_memory.unreachable_at_startup")
        if os.getenv("JOB_QUEUE", "durable").strip().lower() == "memory":
            # in-process delivery: no job table, but a crash can lose a sync or curation
            ledger_events.subscribe(graph_sync.on_ledger_changed)
        else:
            # durable: graph sync and curation are jobs written in the same transaction as the
            # change, run by workers here (and in every other process) with retries
            worker = JobWorker(JobQueue(get_db), {"ledger_changed": graph_sync.handle_job,
                                                  "curate": curator.handle_job},
                               engine, concurrency=int(os.getenv("JOB_WORKERS", "2")),
                               expected_errors=(GraphMemoryError,))
            ledger_events.use_outbox(job_queue.stage, worker.wake)
            curator.use_outbox(job_queue.stage, worker.wake)
            await worker.start()
    try:
        yield
    finally:
        ledger_events.unsubscribe(graph_sync.on_ledger_changed)
        ledger_events.use_outbox(None)
        curator.use_outbox(None)
        if worker:
            await worker.stop()  # finishes jobs in hand; the rest stay queued for next time
        # let in-flight in-process curation and syncs finish before closing the store
        await curator.drain()
        await ledger_events.drain()
        await memory.close()


# The interactive API docs map every endpoint: handy in development, an unnecessary gift to
# an attacker in production. Off unless ENABLE_API_DOCS is set.
API_DOCS = os.getenv("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes", "on")
app = FastAPI(title="AI Book-Keeper", lifespan=lifespan,
              docs_url="/docs" if API_DOCS else None, redoc_url=None,
              openapi_url="/openapi.json" if API_DOCS else None)
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

for module in (registration, login, logout, whatsapp, statements, whatsapp_auth, dashboard, demo):
    app.include_router(module.router)


# ── security headers ─────────────────────────────────────────────────────────
# On every response. The CSP allows scripts, styles and fonts from this server only: no
# inline scripts, handlers or style attributes anywhere in frontend/, and the font is
# self-hosted, so injected markup can neither run code nor restyle the page. (The customer
# statement page sets its own, stricter CSP.) No framing (clickjacking), no MIME sniffing,
# no camera/mic/location, no cross-origin embedding or reading of our resources. Personal
# data (the API, auth) is never cached. HSTS only on HTTPS deployments (COOKIE_SECURE).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}
DOCS_PATHS = ("/docs", "/openapi.json")  # Swagger UI loads its own scripts from a CDN
NO_STORE_PATHS = ("/api/", "/auth/", "/login", "/logout", "/register", "/demo/", "/webhooks/")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    docs = request.url.path.startswith(DOCS_PATHS)
    for name, value in SECURITY_HEADERS.items():
        if docs and name in ("Content-Security-Policy", "Cross-Origin-Embedder-Policy"):
            continue
        response.headers.setdefault(name, value)  # a route's own choice (e.g. no-referrer) wins
    if request.url.path.startswith(NO_STORE_PATHS):
        response.headers.setdefault("Cache-Control", "no-store")
    if settings.COOKIE_SECURE:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


if os.getenv("APP_PUBLIC_URL", "").startswith("https://") and not settings.COOKIE_SECURE:
    logger.warning("security.cookie_not_secure",
                   hint="APP_PUBLIC_URL is https but COOKIE_SECURE is false: set COOKIE_SECURE=true")


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
    """The landing page."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


@app.get("/app", include_in_schema=False)
async def serve_app():
    """The web app (it shows its own login if there's no session)."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "app.html"))

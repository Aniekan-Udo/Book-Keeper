# AI Book-Keeper — Technical Documentation

Conversational bookkeeping for Nigerian small businesses. Users describe what happened
in plain English or Pidgin ("Mama Chidi buy 3 crates of egg, 15k, she pay 10k"); the app
records it, tracks who owes whom, and answers questions about the business.

See `ROADMAP.md` for where the product is going.

---

## 1. Architecture

```
browser (frontend/index.html)
   │  Bearer access token + httpOnly refresh cookie
   ▼
FastAPI (main.py)
   ├── endpoints/        register, login, refresh, logout
   ├── /api/v1/message   ┐
   ├── /webhooks/whatsapp┴→ tools/assistant.py (memory) → tools/router.py: LLM agent (llm.py)
   └── /api/v1/clarify   → fill in a missing amount directly, no LLM
                                   │
tools/clean_version.py  agent tools: extract, report, query, undo, correct
tools/ledger.py         the money rules — recording, debt settlement, undo, corrections
tools/generate_PnL.py, expenses.py, recievables.py   reports
                                   │
Postgres (db.py models, migrations/ via Alembic)
```

The LLM only decides *which* tool to call and with what arguments. Every rule about
money lives in `tools/ledger.py`, is deterministic, and is covered by
`tools/test/test_ledger.py`. Keep it that way: never let the model compute balances.

---

## 2. The ledger

### Transactions (`business_transactions`)

| type | direction | what it means | `outstanding` means |
|---|---|---|---|
| `sale` | in | sold goods/services | customer still owes the user |
| `expense` | out | bought goods/services | user still owes the supplier |
| `loan_given` | out | user lent money | borrower still owes the user |
| `loan_taken` | in | user borrowed money | user still owes the lender |
| `debt_repayment` | in / out | settles an earlier debt | — |

Money columns are `Numeric(18,2)`. `amount` is the full agreed amount; `amount_paid` is
how much of it has been settled (upfront part-payment plus any repayments allocated to
it); `outstanding = max(amount − amount_paid, 0)`.

Defaults when the user doesn't mention a part-payment: sales and expenses are paid in
full; loans are fully outstanding.

A transaction with no stated amount is stored with `status = "needs_amount"` and excluded
from reports until the amount is filled in (via `/api/v1/clarify` or a correction).

### Debt settlement

A `debt_repayment` settles the same party's oldest open entries first (FIFO):
repayments *in* settle sales and loans given; repayments *out* settle expenses and loans
taken. Party names match case- and whitespace-insensitively. Each settlement is recorded
in `repayment_allocations` (repayment → entry → amount) so every balance is auditable and
reversible. Money that doesn't match an open debt is kept on the repayment and flagged in
the reply.

### Undo and corrections

- Every message's entries share a `batch_id`; "cancel that" soft-deletes (`is_deleted`)
  the whole latest batch. Specific entries can be removed by id.
- Deleting a repayment reverses its allocations. Deleting a debt that had repayments
  re-applies those repayments to the party's other open debts.
- Corrections reverse the affected allocations, change the entry, recompute it, and
  re-apply — so balances stay consistent whatever changed (amount, party, date…).

### Reports

- **P&L** is accrual: a sale counts when it's made. The summary also states how much of
  those sales is unpaid, so revenue isn't mistaken for cash.
- **Receivables**: open sales + loans given, grouped by party.
- **Payables**: open expenses + loans taken, grouped by party.
- **Expenses**: total and top items.

All reports exclude deleted and `needs_amount` entries.

---

## 3. Authentication

- `POST /register` — creates the account (email, password, business name, optional
  Nigerian phone normalized to `+234…`) and logs straight in.
- `POST /login` — email (case-insensitive) + password.
- Both return a short-lived **access token** (JWT, default 30 min) in the body and set a
  **refresh token** (JWT, 7 days, `httpOnly` cookie, stored by `jti` in `refresh_tokens`).
- `POST /refresh` rotates the refresh token. Presenting an already-rotated token is
  treated as theft: all the user's sessions are revoked.
- `POST /logout` revokes the current refresh token and clears the cookie.

Every bookkeeping endpoint depends on `get_current_user` (`authentication/auth.py`):
the user is taken from the access token, **never** from the request body. Refresh tokens
carry `type: "refresh"` and are rejected as access tokens.

Rate limits are per user when a valid token is present and per IP otherwise
(`utils.user_or_ip_key`) — Nigerian mobile carriers put many users behind one IP.

---

## 4. The LLM

`llm.py` builds the chat model from `LLM_MODEL` (`provider:model`, default
`google_genai:gemini-2.5-flash`) via LangChain's `init_chat_model`, so switching provider
is a config change. `tools/router.py` runs the agent with a circuit breaker: after 3
consecutive failures it fails fast for 2 minutes instead of making every user wait on
timeouts. Provider rate limits surface as a 503 with a "try again shortly" message.

The system prompt (`tools/prompts.py`) holds the disambiguation examples — English and
Pidgin — that decide sale vs. expense vs. repayment. When a real message is recorded
wrong, add it to `tools/evals.py` and fix the prompt until it passes:

```bash
uv run python -m tools.evals          # live model calls, not part of pytest
```

---

## 5. Channels and conversation memory

Every channel goes through `tools/assistant.respond()`, so web chat and WhatsApp behave
identically and share one memory per user.

**Memory.** The last 8 turns from the past 30 minutes (`conversation_turns`) are passed
to the model, plus a list of entries still waiting for an amount, with their ids. That is
what lets a bare "15k" fill in the rice sale the assistant just asked about. Only user
text and final replies are stored, not tool calls. Turns for one user are processed one
at a time (per-process lock).

**Dates** are Nigerian time (`config.business_today()`, Africa/Lagos) regardless of the
server's timezone.

### WhatsApp (`channels/whatsapp.py`)

1. In the Meta developer dashboard, add the WhatsApp product, register a business number,
   and create a permanent system-user token with `whatsapp_business_messaging`.
2. Set `WHATSAPP_VERIFY_TOKEN` (any string you choose), `WHATSAPP_APP_SECRET`,
   `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `APP_PUBLIC_URL`.
3. Webhook callback URL: `https://<domain>/webhooks/whatsapp`, verify token as above,
   subscribe to `messages`.

Inbound flow: verify the `X-Hub-Signature-256` HMAC → record the message id in
`whatsapp_inbound_messages` (unique — Meta redelivers webhooks) → return 200 at once →
answer in a background task. Senders are matched to accounts by phone (`+234…`);
unknown numbers get a sign-up link that pre-fills their number. Voice notes and other
media get a polite "text only for now" reply.

Trade-off: a message is marked as taken before it's answered, so a crash mid-answer
drops that reply rather than risking a double-recorded sale.

Replies are only free-form within 24 hours of the user's last message (WhatsApp rule).
Proactive messages — debt reminders, weekly summaries — will need approved templates.

---

## 6. Idempotency

`/api/v1/message` accepts an `Idempotency-Key` header. The response is stored in
`idempotency_records` and replayed on retry, so a flaky connection resending a message
doesn't record a sale twice.

---

## 7. Running it

```bash
cp .env.example .env         # fill in SECRET_KEY, GOOGLE_API_KEY, Postgres credentials
docker compose up --build    # runs migrate.py, then serves on :8001
```

Without Docker: set `POSTGRES_URI`, then `uv run python migrate.py` and
`uv run uvicorn main:app --reload`.

### Migrations

`migrate.py` is safe to run on every deploy. It creates a fresh database from the
models, upgrades a database built by the old `create_tables.py` (stamping the baseline
first), or applies pending Alembic migrations. For schema changes:

```bash
uv run alembic revision --autogenerate -m "describe the change"
# review the generated file — autogenerate misses things, especially enums
uv run python migrate.py
```

The old payment tables (`transactions`, `step_up_verifications`) are no longer used but
are not dropped automatically; drop them by hand once you're sure.

### Tests

```bash
uv run pytest                                         # SQLite, no services needed
TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest   # against Postgres
```

---

## 8. Production checklist

- `SECRET_KEY` from a secrets manager, unique per environment.
- `COOKIE_SECURE=true` (HTTPS).
- `ALLOWED_ORIGINS` only if the web client is on another origin.
- A paid LLM quota sized for traffic (the circuit breaker is not a substitute).

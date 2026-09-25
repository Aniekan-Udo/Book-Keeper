# AI Book-Keeper — Technical Documentation

Conversational bookkeeping for Nigerian small businesses. Users describe what happened
in plain English or Pidgin ("Mama Chidi buy 3 crates of egg, 15k, she pay 10k"); the app
records it, tracks who owes whom, and answers questions about the business.

See `ROADMAP.md` for where the product is going.

---

## 1. Architecture

```
browser (frontend/index.html)
   │  httpOnly session cookie (server-side session)
   ▼
FastAPI (main.py)
   ├── endpoints/        register, login, logout, WhatsApp login/connect
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

There are two kinds of account, and one rule that protects both.

**The rule: `user.phone` is only ever set from proof.** The phone number decides whose
books a WhatsApp message goes into, so it's never taken from a form. It's set either by
signing up on WhatsApp (the message itself proves the number) or by sending a code from
WhatsApp (below). Web sign-up has no phone field.

**WhatsApp accounts** (most traders) are created in the chat — see §5, "Sign-up on
WhatsApp" — and have no email or password.

**Web accounts** use email + password:
- `POST /register` — email, password, name, business name; logs straight in.
- `POST /login` — email (case-insensitive) + password. Accounts without a password
  always fail here.

**Proving a phone number with a WhatsApp code** (`tools/phone_auth.py`,
`endpoints/whatsapp_auth.py`). Instead of us texting a code, the website shows one and
the user sends it to our WhatsApp number — no SMS provider or approved template needed:
- `POST /auth/whatsapp/login {phone}` → `LOGIN 483920` plus a `wa.me` link that
  pre-fills it. Sent from that phone, it logs the browser in (passwordless web login).
- `POST /auth/whatsapp/link {phone}` (logged in) → `LINK 483920`. Sent from that phone,
  it connects the number to the web account ("Connect WhatsApp").
- `POST /auth/whatsapp/challenges/<id>` — the page polls; an approved login returns a
  session once.
- A code only counts when it comes **from the number the challenge names**, so guessing
  codes from another phone does nothing, and someone who knows your number can't attach
  it to their account. Codes expire after 10 minutes. The website never says whether a
  number has an account; that's only told to the number's owner, on WhatsApp.
- Replying **LOGOUT** on WhatsApp signs the account out of every web session.

**Sessions**, however you log in, are server-side (`authentication/auth.py`):
- Logging in sets `bk_session`, a random token in an `httpOnly`, `SameSite=Lax` cookie
  (`Secure` when `COOKIE_SECURE=true`). Page scripts can't read it. Only its SHA-256 is
  stored (`sessions`), so a database leak doesn't hand out working logins.
- A session lasts `SESSION_IDLE_DAYS` (30) without a visit, and `SESSION_MAX_DAYS` (90)
  at most. Every login gets a fresh token and replaces the browser's previous session.
- `POST /logout` deletes the session; WhatsApp LOGOUT deletes all of the user's. Both
  take effect on the very next request — there are no tokens left to run out.

Every bookkeeping endpoint depends on `get_current_user`, which looks the session up on
each request: the user comes from the session, **never** from the request body.

**CSRF.** Because logins are cookies, `main.py` refuses any POST/PUT/PATCH/DELETE whose
`Origin` (or, failing that, `Referer`) is another site — on top of `SameSite=Lax`. A
request with neither header (curl, server-to-server) is allowed; browsers always send
`Origin` on cross-site writes. Same-site is judged by the `Host` header, so a reverse
proxy must pass `Host` through unchanged (nginx: `proxy_set_header Host $host;`). Meta's
webhook is exempt (it's verified by signature). Extra origins go in `ALLOWED_ORIGINS`.

Rate limits are per session for logged-in endpoints and per IP for everything before
login (`utils.user_or_ip_key`) — Nigerian mobile carriers put many users behind one IP,
and pre-login endpoints must not be dodgeable with a made-up cookie.

---

## 4. The LLM

`llm.py` builds the chat model from `LLM_MODEL` (`provider:model`, default
`google_genai:gemini-3.8-flash`) via LangChain's `init_chat_model`, so switching provider
is a config change. `tools/router.py` runs the agent with a circuit breaker: after 3
consecutive failures it fails fast for 2 minutes instead of making every user wait on
timeouts. Provider rate limits surface as a 503 with a "try again shortly" message.

The system prompt (`tools/prompts.py`) holds the disambiguation examples — English and
Pidgin — that decide sale vs. expense vs. repayment. When a real message is recorded
wrong, add it to `tools/evals.py` and fix the prompt until it passes:

```bash
uv run python -m tools.evals          # live model calls, not part of pytest
```

### Observability: Opik (`observability.py`)

With `OPIK_API_KEY` + `OPIK_WORKSPACE` set (or `OPIK_URL_OVERRIDE` for self-hosted Opik),
every trader message becomes one Opik trace named "trader message", in the thread
`user-<id>` (so a trader's conversation reads as one thread) and tagged with the channel.
Inside it: every model call with the full prompt it saw, and every tool call with its
arguments and result — enough to see *why* the bot recorded something wrong. Model calls
made inside tools (e.g. parsing a forwarded alert) nest under the same trace.

Each assistant reply stores its trace id (`conversation_turns.trace_id`), and the pilot
report prints it next to every flagged exchange: search that id in Opik to open it.

Not traced: voice notes and screenshots are transcribed before the agent runs, so raw
audio and images never go to Opik — only the resulting text. Off by default; tests
never send traces. Eval runs go to a separate project, `book-keeper-evals`.

Traces contain traders' messages and figures and live on Comet's servers (unless
self-hosted): the privacy notice must list it before you enable it.

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
answer in a background task. Messages from one sender are handled one at a time, in
order (a per-process lock — with several server workers, route each sender to one worker
or move the lock to Redis/Postgres). In order of precedence, a message is: a `LOGIN`/`LINK`
code; a reply from someone who received a payment reminder; a message from a trader
(matched by their proven phone); or someone new, who goes into sign-up.

### Sign-up on WhatsApp (`channels/onboarding.py`)

A new number gets a welcome and two questions — business name, then their name — and
the account is created only once both are answered (so a stray "hi" doesn't make one).
The WhatsApp number becomes the account; there's no email or password. If their very
first message was a transaction ("I sold rice to Emeka 5k"), it's recorded as soon as the
account exists. "cancel" stops; an abandoned sign-up restarts after 24 hours.

The welcome links to `PRIVACY_POLICY_URL`. **Set this before launch**: the Nigeria Data
Protection Act 2023 expects people to be told how their data is used when it's collected.

### Voice notes (`tools/transcribe.py`)

Voice notes are downloaded from Meta (media id → short-lived URL, both with the access
token; capped at ~400 KB, about 2–3 minutes), transcribed by `TRANSCRIPTION_MODEL`
(default Gemini, which accepts audio) into structured output — transcript in the
language spoken, language, English translation for Yoruba/Hausa/Igbo, and whether any
speech was audible — then handled exactly like a typed message prefixed `(voice note)`.
The reply starts with `🎤 “what I heard”` so a misheard name or amount is obvious.
The prompt tells the assistant to confirm implausible names/amounts from voice notes
rather than record a guess.

Formats: WhatsApp's `audio/ogg` (Opus), plus mp3, aac, wav, flac. `audio/amr` isn't
supported by Gemini and gets a "record it as a normal voice note" reply.

Check transcription against real recordings:

```bash
uv run python -m tools.transcribe note1.ogg note2.ogg
```

Trade-off: a message is marked as taken before it's answered, so a crash mid-answer
drops that reply rather than risking a double-recorded sale.

Replies are only free-form within 24 hours of the user's last message (WhatsApp rule).
Proactive messages — debt reminders, weekly summaries — will need approved templates.

---

### Debt collection (`tools/debt_collection.py`)

**Data.** Debtor phone numbers live in `contacts` (per trader, keyed by the same
normalised name used to group debts), saved from "Mama Chidi number na 0803…" or given
alongside a sale. Promised payment dates are `due_date` on the entry ("she go pay
Friday"); the receivables report calls out overdue debts.

**Reminders.** "Remind Mama Chidi" drafts a reminder (`reminders`, status `draft`) and
shows the trader the recipient and the exact message. It is sent only by
`confirm_reminder_tool` in a *later* request — the server refuses to send a draft
created during the current request, whatever the model does. Before drafting and again
before sending, the server checks:

- the debtor hasn't opted out (`reminder_opt_outs`, global across traders);
- no reminder went to that number from this trader in the last `REMINDER_COOLDOWN_DAYS` (3);
- the trader hasn't sent `REMINDER_DAILY_LIMIT` (20) in the last 24 hours;
- at send time, the balance still matches the draft (if it was paid or changed, it isn't sent).

Drafts expire after 30 minutes. Failed sends are recorded as `failed`.

**Replies from debtors.** A number that has received a reminder and replies `STOP` is
opted out for good. Any other reply gets an automatic acknowledgement, and is forwarded
to the trader ("💬 Mama Chidi replied…") if the trader has messaged us on WhatsApp in
the last 24 hours (WhatsApp won't allow it otherwise). Strangers still get the sign-up
link.

**The template.** Reminders go to people who never messaged the business number, so
WhatsApp only allows an approved template. In Meta Business Manager → WhatsApp Manager →
Message templates, create:

| Field | Value |
|---|---|
| Category | Utility |
| Name | `payment_reminder` (set `WHATSAPP_REMINDER_TEMPLATE` to whatever you choose) |
| Language | English (`en`, or set `WHATSAPP_REMINDER_TEMPLATE_LANG`) |
| Body | `Hello {{1}}, this is a friendly reminder from {{2}}. You have an outstanding balance of {{3}} for {{4}}. Please contact them to arrange payment. Thank you! Reply STOP to stop these reminders.` |
| Samples | `Mama Chidi`, `Ada Provisions`, `₦5,000.00`, `3 crates egg (Sep 03)` |

`REMINDER_TEMPLATE_TEXT` in `tools/debt_collection.py` must match the approved body —
it's what the trader is shown before confirming. Until the template is set, reminders
are off and the assistant says so.

Reminders come from the Book-Keeper number, not the trader's own, which is why the
business name is in the message. Keep the cooldown and cap conservative: users blocking
or reporting the number lowers its quality rating and can get it restricted.

### Customer statements (`tools/statements.py`, `endpoints/statements.py`)

"Send me Mama Chidi statement" builds a statement of account between the trader and
one person: every sale, loan, supplied good and payment with them in date order, with a
running balance and the promised due date on each debt. The balance is "what they owe
the trader" — negative when the trader owes them (suppliers). A start date brings the
earlier balance forward. Payments made on the spot are shown separately from later
repayments (upfront = `amount_paid` − repayment allocations), and the closing balance
always equals the ledger's open balances for that person (tested as an invariant).

The assistant replies with a short text version for forwarding and a private link,
`/s/<token>`, to a printable page:

- token from `secrets.token_urlsafe(24)`; one active link per person, reused and
  extended; expires after 30 days; "stop sharing Mama Chidi statement" revokes it;
- the page shows only that person's dealings, rendered live (always current);
- everything the trader typed is HTML-escaped; `noindex`, `no-store`, `no-referrer`;
  rate-limited; unknown/expired/revoked tokens get the same plain 404;
- no way to pay on it — a record, not a payment request (the app never touches money).

Links need `APP_PUBLIC_URL`; without it the assistant still gives the text version.

### Bank and POS alerts (`tools/alerts.py`)

Traders forward or paste alerts (SMS, email, app notification) or send a screenshot of
one. Strictly read-only: we parse what they choose to send — no bank connections, no
logins, and the assistant never asks for PINs, OTPs, BVNs or card numbers.

The model (`TRANSCRIPTION_MODEL`, which also reads screenshots) only extracts fields:
credit/debit, amount, counterparty, narration, date, reference, and whether it's a
sender's "transfer successful" receipt or contains a secret. What happens next is
decided in code:

| Situation | Result |
|---|---|
| Money in from the one person whose open debts cover the amount (or money out to the one supplier you owe) | Recorded as a repayment immediately; undoable |
| No match, several matches, or more than they owe | Question: sale / old debt (whose) / loan / not business |
| Same amount as an entry already recorded ±1 day | Question: "same money as #12?" — avoids double counting |
| Same alert forwarded again | Skipped (fingerprint: reference, else date+name+narration) |
| Sender's "transfer successful" screen | Never auto-recorded; the trader is told it isn't proof the money arrived |
| Contains a PIN/OTP/card number/BVN | Not stored; the user is warned |

When the trader answers "that's Mama Chidi", the bank name is remembered
(`party_aliases`), so her next transfer is recognised automatically. Only parsed fields
are stored (`bank_alerts`) — not the raw text, balances or account numbers. Undoing an
entry made from an alert frees that alert to be used again.

Try real alerts: `uv run python -m tools.alerts "Credit Alert ..."` or
`uv run python -m tools.alerts screenshot.jpg`.

## 6. Idempotency

`/api/v1/message` accepts an `Idempotency-Key` header. The response is stored in
`idempotency_records` and replayed on retry, so a flaky connection resending a message
doesn't record a sale twice.

---

## 7. Running it

```bash
cp .env.example .env         # fill in GOOGLE_API_KEY, Postgres credentials, WhatsApp settings
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

- Secrets (API keys, WhatsApp token/app secret, DB password) from a secrets manager.
- `COOKIE_SECURE=true` (HTTPS).
- `ALLOWED_ORIGINS` only if the web client is on another origin.
- A paid LLM quota sized for traffic (the circuit breaker is not a substitute).

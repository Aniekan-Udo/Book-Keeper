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
   ├── /webhooks/whatsapp┴→ tools/assistant.py: recall → agents/ (Recorder | Collector |
   │                        Alert matcher, LLM via tools/router.py) → memory curator (§9)
   └── /api/v1/clarify   → fill in a missing amount directly, no LLM
                                   │
tools/clean_version.py  agent tools: extract, report, query, undo, correct
tools/ledger.py         the money rules — recording, debt settlement, undo, corrections
tools/generate_PnL.py, expenses.py, recievables.py   reports
                                   │
Postgres (db.py models, migrations/ via Alembic)        ← source of truth for money
   │ after each commit: tools/ledger_events → memory/sync (background)
   ▼
FalkorDB (memory/)  one graph per trader: parties, debts, roles; learned memory (§9)
```

The LLM only decides *which* tool to call and with what arguments (and, in the curator,
proposes memories that code checks). Every rule about
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

`llm.py` picks three models by config (`provider:model`, via LangChain's
`init_chat_model`), so switching provider is a config change:

| Setting | Used for | Default |
|---|---|---|
| `LLM_MODEL` | the agent; parsing forwarded alert text | `groq:openai/gpt-oss-120b` |
| `VISION_MODEL` | reading alert screenshots (must accept images) | `groq:qwen/qwen3.8-27b` |
| `TRANSCRIPTION_MODEL` | voice notes | `groq:whisper-large-v3` |

Groq needs `GROQ_API_KEY`. Any of the three can be Gemini instead
(`google_genai:gemini-3.8-flash`, with `GOOGLE_API_KEY`). `tools/router.py` runs the agent with a circuit breaker: after 3
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

**Long-term memory** (who people are, what they promised, what the trader wants) lives
in the graph, not in conversation turns. See §9.

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
into a transcript in the language spoken, the language, an English translation when it
isn't English, and whether any speech was audible — then handled exactly like a typed
message prefixed `(voice note)`.

- **Groq Whisper** (default, `groq:whisper-large-v3`): Groq's speech-to-text API, with a
  prompt that nudges spelling towards traders' vocabulary ("45k", "balance", "Mama
  Chidi"); non-English speech also goes through Whisper's translate-to-English. Whisper
  has no Pidgin (it hears it as English, which is fine) and no Igbo.
- **A chat model that listens** (e.g. `google_genai:gemini-3.8-flash`): reads the audio
  directly and follows instructions (amounts as "15k", keep Pidgin as spoken, Igbo).
The reply starts with `🎤 “what I heard”` so a misheard name or amount is obvious.
The prompt tells the assistant to confirm implausible names/amounts from voice notes
rather than record a guess.

Formats: WhatsApp's `audio/ogg` (Opus), plus mp3, aac, wav, flac. `audio/amr` isn't
supported and gets a "record it as a normal voice note" reply.

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

The model (`LLM_MODEL` for text, `VISION_MODEL` for screenshots) only extracts fields:
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
cp .env.example .env         # fill in GROQ_API_KEY, Postgres credentials, WhatsApp settings
docker compose up --build    # runs migrate.py, then serves on :8001
```

Without Docker: set `POSTGRES_URI`, then `uv run python migrate.py` and
`uv run uvicorn main:app --reload`. For graph memory, also run FalkorDB:
`docker compose up -d falkordb` (or set `GRAPH_MEMORY=off`).

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

The FalkorDB adapter tests in `test/test_graph_memory.py` run against a real FalkorDB
(`docker compose up -d falkordb`) and are skipped if none answers at
`FALKORDB_TEST_HOST:FALKORDB_TEST_PORT` (default `127.0.0.1:6379`). They use their own
`test_trader_*` graphs and delete them.

---

## 8. Production checklist

- Secrets (API keys, WhatsApp token/app secret, DB password) from a secrets manager.
- `COOKIE_SECURE=true` (HTTPS).
- `ALLOWED_ORIGINS` only if the web client is on another origin.
- A paid LLM quota sized for traffic (the circuit breaker is not a substitute).
- `ENABLE_API_DOCS=false` and `DEMO_ACCOUNTS=false` (both default off); `COOKIE_SECURE=true`
  (the app warns at startup if the site is https without it).
- Rebuild the image regularly for base-image security fixes; re-run the scans in D47.
- FalkorDB: `FALKORDB_BROWSER=0` (or keep port 3000 bound to 127.0.0.1 only, as compose
  does), a password (`FALKORDB_PASSWORD` plus `--requirepass` on the server), and backups
  of the `falkordb_data` volume — learned memory can't be rebuilt from Postgres.

---

## 9. Graph memory (FalkorDB)

Built for Graph Hacks (WeMakeDevs × FalkorDB, Oct 15–18 2026), Track 2: Agent Memory and
Coordination. Product design, the four agents and the demo scenario:
`docs/hackathon-design.md`, `docs/hackathon-scenario.md`. This section records how it's
built and why.

**Goal.** The agent today forgets everything after 30 minutes (§5). Graph memory lets it
learn a trader's business over time (who "Chidi's mum" is, who keeps promises, what "the
usual" means, who not to chase) and lets several agents share that knowledge, with every
fact traceable to the message it came from.

**Status.**

| Day | Built |
|---|---|
| 1 (Oct 15) | FalkorDB in compose; the GraphMemory port and FalkorDB adapter; ledger → graph sync; `rebuild_graph` |
| 2 (Oct 15) | Three agents (Recorder, Collector, Alert matcher) on a LangGraph StateGraph with plain-code routing; recall (memory in every prompt); the Memory curator; promises, aliases, usual orders, do-not-remind, stated roles; two code-enforced guards found in live runs |
| 3 (Oct 15) | Collector: overdue promises settled (broken/kept), "who to chase" ranked by code with reasons, do-not-remind enforced in the reminder tool, sent reminders in the graph with BECAUSE links; Alert matcher learns and uses bank names; the "why?" tool. Graphiti stretch goal not taken: the time went into live-run fixes |
| 5b (Oct 16) | Bookkeeping first: Books is the home screen (today / this week / this month / last month / all time, by item), new Owed & owing page (what for, amount, paid, balance, due date, overdue), landing page leads with the books; demo month shifted by whole weeks to end this week |
| 5 (Oct 16) | Web app for judges: landing page (`/`), the app (`/app`) with Chat, Overview, Who to chase, People & memory (provenance per memory), read-only views in `endpoints/dashboard.py`, one-click demo login (`DEMO_ACCOUNTS`) |
| 4 (Oct 15) | Demo: `seed_demo` (the 30-day scenario through the real code, two accounts), `demo_check` (side by side); README for the submission; hardening from the rehearsal (turn timeout, model-slip retry, graph keepalive and read retries) |

### 9.1 Architecture

```
tools/ledger.py ──commit──► tools/ledger_events.py      LedgerChanged(user_id, party_keys, reason)
                                   │ background task, per subscriber
                                   ▼
                            memory/sync.py  GraphSync   reads Postgres → PartySnapshot[]
                                   │
                                   ▼
                            memory/ports.py GraphMemory (Protocol)
                              ├── memory/falkor.py  FalkorGraphMemory → FalkorDB graph trader_<id>
                              └── memory/null.py    NullGraphMemory   (GRAPH_MEMORY=off, tests)

main.py lifespan: build_graph_memory() → set_graph_memory() → subscribe GraphSync
scripts/rebuild_graph.py: GraphSync.rebuild() for one or all traders
```

The ledger doesn't import anything from `memory/`. It only publishes events. The
dependency points one way: `memory → tools.ledger_events`.

**One trader message** (`tools/assistant.respond`):

```
message
  │
  ├─ recall (memory/recall.py)        code only: find known/possible people in the message,
  │                                   fetch what the graph remembers → prompt section
  ├─ route (agents/routing.py)        code only: alert text → Alert matcher; "remind"/"chase"
  │                                   → Collector; answer to a pending item → its agent; else Recorder
  ├─ agent team (agents/team.py)      LangGraph StateGraph: the chosen agent runs with its own
  │     recorder | collector |        prompt and tools; it may hand off once (transfer_to_agent)
  │     alert_matcher                 ledger tools → Postgres → LedgerChanged → GraphSync
  ├─ reply saved (conversation_turns)
  └─ curator (memory/curator.py)      background: promises from due dates, inferred usual
                                      orders (code); aliases, usual orders, do-not-remind,
                                      roles (model proposes, code checks, Cypher writes)
```

The agents never message each other. What one learns reaches the others through the
graph: the curator writes "don't remind Pastor John" after a Recorder or Collector turn,
and the Collector reads it on the next one. Every memory carries `WRITTEN_BY` (which agent)
and `LEARNED_FROM` (which message).

Agents' tools don't write memory directly: they leave **notes** for the turn (a bank name
the trader confirmed, a reminder that was sent), and the curator writes them once the
message is saved, so they too carry `LEARNED_FROM` the message, with the Alert matcher or
Collector as `WRITTEN_BY`.

Coordination in the graph, concretely:

| Written by | Read by | Example |
|---|---|---|
| Curator: promise (from a due date) | Sync marks it kept; Collector marks it broken; Collector ranks on it | Emeka first: "broken 3 of 4" |
| Curator: alias | Recall (every agent), the record tool's name check | "Chidi's mum pay 30k" → Mama Chidi, no question |
| Alert matcher (note): bank name | Alert matcher, next alert | `NWOSU EMEKA CHUKWUDI` → Emeka, no question |
| Curator: do-not-remind | Collector: ranking (skipped, with reason) and the reminder tool (refuses) | Pastor John skipped |
| Collector: promise broken, reminder sent | "Why?" tool, next ranking | "Why Emeka first?" |

### 9.2 Design patterns, and why each one is there

| Pattern | Where | Why |
|---|---|---|
| **Ports and adapters** | `memory/ports.py` (port), `memory/falkor.py`, `memory/null.py` (adapters) | App code depends on what it needs ("sync these parties"), not on FalkorDB. The engine can be swapped or faked; all Cypher sits behind one interface |
| **Observer / domain events** | `tools/ledger_events.py` | The money rules stay free of graph code. A slow or failing listener can't delay or break recording a sale |
| **Null Object** | `NullGraphMemory` | "Memory off" is a real implementation, so callers never branch on `if memory is None` |
| **Factory** | `memory.build_graph_memory()` | One place reads the environment and picks the adapter; the FalkorDB driver is only imported when used |
| **Dependency injection** | `GraphSync(memory, session_factory)` | Production passes `db.get_db`; tests pass their rolled-back session and a fake store |
| **Snapshot reconciliation** | `party_snapshots()` → `sync_parties()` | The graph is made to *match* the ledger for a party rather than replaying diffs. Idempotent, so live sync and full rebuild are the same code path, and a missed event is fixed by the next one |
| **State machine** | `agents/team.py` (LangGraph `StateGraph`) | The turn's control flow is explicit: route → agent → (handoff → agent) → end, with a hard limit of one handoff, so agents can't bounce a message back and forth |
| **Specification (table-driven agents)** | `AGENT_TOOLS`, `PROMPTS` in `agents/team.py` | An agent is data: a tool list and a prompt builder. Adding one is a table entry, and a test checks each agent sees only its own tools |
| **Command objects + validator** | `MemoryOp` → `Curator._apply` | The model proposes typed commands; code validates each one (does the person exist? was the alias said?) and runs fixed Cypher. The model never writes to the graph |
| **Guard in code, not in the prompt** | `could_be` in the record tool; `claims_unbacked_recording` in the team | Two failure modes seen in live runs are refused or retried by code: splitting a customer across two names, and claiming a recording that never happened |
| **Background worker with one retry** | `Curator.schedule`, `GraphSync.on_ledger_changed` | Off the reply path; a blip in the graph store gets one delayed retry, which is safe because both are idempotent |
| **Deferred writes (notes)** | `memory_notes` in `build_tools` → `Turn.notes` → `Curator._apply_notes` | Tools run before the message is saved, so they can't know its id; they hand memory writes to the curator, which has it. One place writes memory, with provenance |
| **Deterministic scoring** | `Collector.who_to_chase` | The ranking is a sort key over facts (broken promises, days overdue, amount, age), so it's reproducible and every position has stated reasons |

Deliberately **not** used:
- **A message queue** (Redis streams, Celery). The events are in-process tasks. A queue would
  survive a crash, but a rebuild already recovers anything lost, and a queue is one more
  service to run during a 4-day build.
- **Repositories per node type.** One adapter with named query constants keeps every
  statement reviewable in one file.

### 9.3 Graph model (implemented so far)

One graph per trader, named `trader_<user_id>`.

```
(:Trader {id})
(:Party {key, name, created_at})
(:Debt {pg_id, kind, open, date, due, settled_on}) -[:WITH]-> (:Party)
(:Party) -[:CUSTOMER_OF|SUPPLIER_OF {confidence, valid_from, valid_to, turn_id, written_by}]-> (:Trader)

(:Alias {key, text, kind}) -[:REFERS_TO {valid_from, valid_to}]-> (:Party)
(:Promise {id, due, status, paid_fraction, made_on, closed_on}) -[:FOR]-> (:Debt)
                                                                -[:MADE_BY]-> (:Party)
(:Fact {id, kind, summary, said, data, until, confidence, valid_from, valid_to}) -[:ABOUT]-> (:Party)

(:Reminder {pg_id, sent_at}) -[:SENT_TO]-> (:Party), -[:BECAUSE]-> (:Promise)

(:Alias|:Promise|:Fact|:Reminder) -[:LEARNED_FROM]-> (:Message {turn_id, at})   // the trader's message
(:Alias|:Promise|:Fact|:Reminder) -[:WRITTEN_BY]->   (:Agent {name})            // curator / alert_matcher / collector
```

- `Party.key`: the normalised name, the same grouping the ledger uses for debts
  (`lower(trim(party))`).
- `Debt.kind`: `receivable` (they owe the trader) or `payable` (the trader owes them).
  `settled_on`: the date of the repayment that cleared it.
- `Alias.kind`: `nickname`, `transcription` (a misheard voice note), later `bank_name`.
- `Promise.status`: `open`, `kept`, `broken`, `replaced` (more time asked *before* the due
  date; not held against them). `closed_by`: who decided (`ledger_sync`, `collector`,
  `curator`); `paid_fraction`: how much of the debt was paid when it was settled.
- `Reminder.pg_id` is the `reminders` row; `BECAUSE` links the promises still open or
  already broken when it was sent.
- `Fact.kind`: `usual_order` (`data` holds the items as JSON) or `do_not_remind` (`until`
  optional). `confidence`: `stated` (the trader said it) or `inferred` (code worked it out).
- `Message.turn_id` is a `conversation_turns` id: the message text stays in Postgres; the
  graph only points at it.
- Unique constraints: `Party.key`, `Debt.pg_id`, `Promise.id`, `Message.turn_id`,
  `Agent.name`, `Reminder.pg_id`. Range index: `Alias.key`.

### 9.4 Decisions and trade-offs

**D1. Postgres keeps the money; the graph keeps structure.** Debt nodes store the Postgres
id, which way the debt goes, and whether it's open, but never amounts.
*Trade-off:* anything that needs a balance (the Collector) asks Postgres. Accepted:
balances must come from the one place the money rules live (§2).

**D2. One graph per trader.** One trader's memory can't leak into another's, even through
a buggy query, and queries need no trader filter.
*Trade-off:* no queries across traders. None are needed; the customer STOP list, which
does span traders, stays in Postgres.

**D3. Sync runs after the commit, in the background.** The reply never waits for the
graph and never fails because of it.
*Trade-off:* the graph lags by milliseconds, and a sync in flight is lost if the process
dies. A failed sync is retried once after 10 s (safe: it's a reconciliation), and
`scripts/rebuild_graph.py` repairs anything beyond that.

**D4. Reconcile whole parties, not diffs.** Each event resyncs the parties it names from
Postgres. A correction that moves an entry from "Bolu" to "Bola" names both, so the debt
moves.
*Trade-off:* every change re-reads all of that party's entries. That's cheap at
market-trader volumes, and correctness is worth more than the saved query.

**D5. Only credit becomes a Debt node:** something still owed, a repayment ever applied,
or a promised date. Cash sales stay out, so the graph stays small. Sales history for
"usual order" is read from Postgres when needed.

**D6. Repaid debts stay, as `open: false`.** Whether someone paid on time is promise
history, which the Collector ranks on.

**D7. Undone entries are removed, with any promise about them.** The party node stays,
since it may carry learned memory (aliases, facts). A party whose entries are all undone
is never *created*: a typo leaves no trace.

**D8. Roles.** Customer is inferred from a first sale or loan given. Supplier is never
inferred: buying from someone (e.g. paying a porter) doesn't make them a supplier. Once
any customer role has been *stated* (even one that was later closed), the ledger stops
inferring: what the trader said beats what the entries suggest.

**D9. Dates are ISO strings** (`2026-09-07`). They compare correctly as text and read
clearly in the FalkorDB Browser.

**D10. A graph outage never breaks bookkeeping, or startup.**
- The driver's constructor makes a blocking connection (it checks for cluster mode), so
  the client is built lazily, in a worker thread, on first use.
- Socket and query timeouts are 2 s.
- Failures become `GraphMemoryError`; the client reconnects on the next call, and the sync
  and the curator each retry once after a delay. Writes are not retried by the driver: a
  timed-out `CREATE` may have happened, and replaying it would duplicate a promise.
- `/health` reports `"graph": "ok" | "unavailable" | "off"` but still returns 200.
- Compose starts the app once FalkorDB has *started*, not waiting for it to be *healthy*.

**D11. Default host is `127.0.0.1`, not `localhost`.** On Windows, `localhost` tries IPv6
first, and Docker publishes the port on IPv4 only, so every connection hung until it timed
out. Found while testing.

**D12. FalkorDB image pinned by digest.** Cypher and constraint behaviour shouldn't change
under us on a rebuild. To upgrade: pull, run the tests, update the digest.

**D13. Logs carry ids and counts, never customer names.** Party names are personal data
about the trader's customers (Nigeria Data Protection Act). An event like
`graph_sync.done user_id=1 parties=2 ms=23` is enough to debug.

**D14. Every Cypher statement is a named, parameterised constant in `memory/falkor.py`.**
Nothing is built from strings, the model never writes Cypher, and the full list (below)
is the reviewable surface that Rule 10 asks submissions to document.

**D15. Rebuild never wipes learned memory unless asked.** `rebuild_graph` resyncs and
prunes. Only `--wipe` (and never together with `--all`) deletes a trader's graph, because
aliases and facts can't be recreated from Postgres.

**D16. Routing is plain code; only a handoff is the model's call.** A forwarded alert and
"remind" are easy to recognise, and a supervisor model would add a call (and tokens) to
every message. A misroute costs one handoff, never a wrong answer. At most one handoff per
message.
*Trade-off:* the routing rules are regexes that need maintaining as phrasing varies; the
handoff is the safety net.

**D17. Each agent has its own short prompt and only its tools.** The Recorder no longer
carries reminder or alert rules, the Collector no recording examples. Fewer tokens per call
and fewer wrong tool choices. Shared money and safety rules go to every agent.

**D18. Recall is code, not a model call.** People are found by matching the message
against known names and aliases (longest match first), in milliseconds. The model only
sees the result, as a short prompt section.

**D19. Promises and "kept" come from the ledger, not the model.** A due date on an entry
*is* a promise; clearing the debt by the due date (dated by the repayment, not the day the
sync runs) *is* keeping it. Code does both. This changed from the design doc, where the
curator model was to extract promises: it's computable, so it's computed.

**D20. The model proposes, code disposes.** The curator's model returns `MemoryOp`s
(aliases, stated usual orders, do-not-remind, roles). Each is validated: the person must
exist; an alias must appear in the conversation; an alias can't swallow a person who has
ledger entries or graph records of their own; a restated fact is a no-op. Rejections are
logged with reasons.

**D21. The curator only calls the model when something could be remembered**: a known or
possible person in the message, a named entry recorded, or a question just answered. "Who
owe me?" costs no curator call.
*Trade-off:* a new nickname for someone never mentioned before, in a message with no entry,
is missed. It'll come up again with an entry.

**D22. The curator can run on its own, cheaper model** (`CURATOR_MODEL`, default
`LLM_MODEL`). It fills in a small form and code checks everything it returns. Its calls
share the provider rate limit with the agents, so a smaller model frees headroom.

**D23. JSON-schema structured output for the curator.** With tool calling, `gpt-oss` on
Groq sometimes called a made-up `commentary` tool, or no tool when there was nothing to
say, and Groq failed the request. Groq's JSON-schema mode avoids both. Providers without it
fall back to tool calling.

**D24. Ambiguous new names are refused in code.** If a new name strongly resembles a known
person ("Chidi's mum" when Chidi and Mama Chidi exist; "Mama C" for Mama Chidi), the record
tool refuses until the user says who they mean, and blocks the candidates for the rest of
that message so the model can't just pick one. A name only counts as confirmed new if the
previous assistant message asked about it.
*Trade-off:* one extra question in genuinely ambiguous cases. A customer's debts split
across two names is much worse.

**D25. Weak resemblance is a hint, never a block, and the hint says "different person".**
"Chidi" when only "Mama Chidi" is known is most likely someone else (her son). Blocking it
stopped a real Chidi being recorded; hinting "maybe the same" made the model file his
₦2,000 under Mama Chidi. Both happened in live runs.

**D26. A reply that claims a recording must be backed by one.** If the Recorder or Alert
matcher says "I don record…" but no ledger tool ran and no entry was written, the team
re-runs it once, telling it nothing was saved. Seen live: "I don add sale: Pastor John…"
with nothing in the ledger.
*Trade-off:* a false alarm (a reply that merely sounds like a claim) costs one model call.

**D27. Two FalkorDB query quirks, worked around** (FalkorDB at the pinned digest):
- After a `WITH … collect()`, an `OPTIONAL MATCH` with any property filter (inline
  `{open: true}` or `WHERE`) matches nothing, while the same match unfiltered works. Recall
  therefore collects values and filters in Python. A regression test covers it.
- Relationship types can't be parameters, so the role queries exist once per type
  (`CUSTOMER_OF`, `SUPPLIER_OF`), chosen from a dict: still constants, never string-built
  from input.

**D28. "Who to chase" is ranked by code.** Sort key: most broken promises, then longest
overdue, then most owed, then owed longest. Reasons are generated from the same facts, so
the answer to "why Emeka first?" matches the ranking exactly. The model only phrases it.
*Trade-off:* the weighting is fixed and simple (broken promises always outrank amount). A
trader may care more about a big debt; easy to change, and the reasons make it visible.

**D29. Broken is decided when the due date passes**, by the Collector's sweep, which runs
before every ranking and daily from cron (`scripts/collector_sweep.py`). Kept is decided
by the sync when the debt clears (D19); a late payment is broken with `paid_fraction` 1.0.

**D30. Do-not-remind is enforced in the reminder tool**, not only in the prompt and the
ranking: drafting is refused until the trader confirms, and the confirmation only counts
if the previous assistant message asked about that person (the same rule as D24).

**D31. Skipped is shown, not hidden.** Someone the trader said not to remind, or who
replied STOP, appears under "skipped" with the reason in the trader's own words, so the
trader can see the assistant remembered.

**D32. Bank names are graph aliases (`kind: bank_name`), PartyAlias stays as fallback.**
The Alert matcher checks the graph first; the old PartyAlias table is still written and
read, so alerts keep matching with graph memory off.

**D33. The "why?" tool answers from provenance, not the model's memory.** It returns every
promise with its outcome, who decided it and the trader's message it came from (text
fetched from Postgres by `turn_id`), plus facts, aliases, roles and reminders with their
reasons. The model turns that into a sentence.

**D34. An empty answer in the wrong shape is "nothing to remember".** On `gpt-oss-20b`,
the curator sometimes replied `[]` instead of `{"ops": []}`, which Groq's JSON check
rejects. The prompt now spells out the shape, and that specific rejection is treated as no
memories, not an error.

**D35. The demo is seeded through the real code, with a scripted model.** `seed_demo`
replays the scenario's 30 days through the ledger, the sync, the curator and the
Collector's sweeps, on the scenario's own dates. Where the curator would ask the model, it
is handed the scenario's expected operations, and still validates and writes them itself.
Free, repeatable, and the graph it builds is the one the product would build.
*Trade-off:* the seeding doesn't test the model's judgement; the live runs (§9.9) and
`demo_check` do.

**D36. Day 1 vs day 30 is the same ledger.** The day-1 account gets identical entries but
no curator, notes or sweeps: its graph holds only what the ledger implies. Any difference
in the replies is what graph memory adds.

**D37. A ceiling on every agent run** (`LLM_TURN_TIMEOUT`, default 90 s). In the rehearsal
an agent run took an hour (most likely the machine sleeping, but a stalled provider would
look the same) and held the trader's per-user lock throughout. Past the ceiling, the
trader is told to try again.

**D38. A malformed tool call is retried once, and isn't an outage.** `gpt-oss` sometimes
writes tool-call JSON Groq can't parse (`tool_use_failed`). One retry; a second failure
asks the trader to rephrase. Neither counts towards the circuit breaker, which is for
provider outages: in the rehearsal, these slips had tripped it.

**D39. Graph connections: keepalive, health checks, read retries.** After a long idle
spell, pooled connections had been dropped silently and every read timed out. Now: TCP
keepalive, a health check on connections idle 30 s, a 5 s timeout, and read-only queries
through `GRAPH.RO_QUERY`, retried once. Writes are still never retried by the driver (D10).

**D40. A web app that shows the memory, at no token cost.** WhatsApp can't be demoed to
judges right now, so the web app is the product's front door. Its Overview, Who to chase
and People & memory pages are plain code over Postgres and the graph
(`endpoints/dashboard.py`): no model calls, so exploring costs nothing and always shows the
same thing. People & memory turns the graph's provenance into something a person can read:
"you said this on 2 Sept · saved by the curator · outcome by the collector on 8 Sept".
*Trade-off:* more endpoints to keep in step with the memory model; they reuse the same
recall, Collector and explain code the agents use, so there's one source of truth.

**D41. Demo login is opt-in per server.** `/demo/login` only exists when
`DEMO_ACCOUNTS=true`, only logs into the two seeded demo accounts (made-up data), and is
rate limited. Production leaves it off.

**D42. No build step for the frontend.** Plain HTML, CSS and JavaScript served by FastAPI:
nothing to install or compile, and the same container serves it. Assistant replies are
escaped and only bold, bullets and line breaks are added back, so a reply can't inject
HTML.

**D43. Bookkeeping leads; memory is the difference.** The product is a bookkeeper: what
you made and spent this week or month, who owes you for what and by when, who you owe.
The app opens on Books, the Owed & owing page lists every open entry with its item,
amount, payments, balance and due date, and the landing page leads with the books.
Memory and debt chasing follow as "and it remembers". The demo month is shifted forward by
whole weeks when seeded (`scripts/seed_demo.py`), so "this week" and "this month" hold real
entries and every weekday in the messages stays true.

**D44. One turn at a time per user, across processes: a Postgres advisory lock.** The
in-process asyncio lock only covered one process. Now `tools/turn_lock.py` also takes
`pg_advisory_lock(0x424B, user_id)` on its own connection for the whole turn: commits made
mid-turn don't release it, and if the process dies Postgres releases it with the
connection. The in-process lock stays as a free first gate. Waiting is bounded
(`TURN_LOCK_TIMEOUT`, 120 s, just over the 90 s agent ceiling); past it, the trader is told
"still working on your last message" (a `TurnInProgress`, shown like any busy reply).
Skipped on SQLite. Tested against real Postgres with two engines standing in for two
processes.
*Trade-off:* each turn holds a second connection while it runs, which D45 sizes for.

**D45. Connection pool sized per process.** It was 100 + 50 overflow per process, above
Postgres's default `max_connections` of 100 on its own. Now `DB_POOL_SIZE` (10) and
`DB_MAX_OVERFLOW` (10), with the rule written down: processes × (pool + overflow) stays
under `max_connections`, leaving room for migrations and admin. One turn uses up to two
connections (its session and the lock), so the defaults allow 10 concurrent turns per
process before overflow.

**D46. Graph sync and curation are durable jobs: a transactional outbox in Postgres.**
In-process background tasks were lost if a process died. Now `jobs/queue.py` writes each
job into the *same transaction* as the change that causes it: a `ledger_changed` job with
the ledger entry, a `curate` job with the saved reply. Saved together or not at all, so
there's no window between "committed" and "queued". Workers (`jobs/worker.py`,
`JOB_WORKERS` per process, woken on commit, polling otherwise) take a user's jobs oldest
first under a per-user advisory lock, so graph sync always runs before the curation that
depends on it and no two processes run one user's jobs at once. A failure backs off (10 s,
30 s, 2 min, 10 min, 30 min) and holds that user's later jobs; after `JOB_MAX_ATTEMPTS` (6)
the job is parked as failed and the queue moves on (`scripts/jobs.py status | failed |
retry | run`). Delivery is at-least-once; the handlers are idempotent. Tests and scripts
keep the in-process delivery behind the same `stage()` / `committed()` interface
(`JOB_QUEUE=memory` selects it in the app too).
*Why Postgres, not Redis or Celery:* the outbox has to share the ledger's transaction,
it's one service fewer to run, and the volume (a few jobs per message) is far below what a
table queue handles.
*Trade-off:* jobs are picked up within the poll interval (2 s) by other processes; the
process that committed is woken at once. Seen live: with FalkorDB stopped, replies kept
working; the sync job failed twice, backed off, then completed by itself about 9 s after
FalkorDB came back, and the curation queued behind it ran right after.

**D47. Security baseline, from scanners until paid tools are affordable** (Oct 16). Run and
fixed:
- **gitleaks** (whole history): no secrets in tracked files. One historic finding: the
  July initial commit had a `.env` with test Postgres credentials and a `SECRET_KEY` that
  nothing uses any more. Low risk; purging history (`git filter-repo` + force-push) is
  optional.
- **pip-audit**: 6 CVEs in cryptography, langgraph-sdk, urllib3 → upgraded; now none.
- **Bandit**: one low-severity false positive (a test password in `tools/evals.py`).
- **Semgrep** (OWASP Top 10, Python, JS, secrets, Dockerfile, JWT): container ran as root →
  unprivileged `app` user; one f-string SQL → bound parameter.
- **Trivy**: base-image CVEs → `apt-get upgrade` at build, unused system pip/setuptools/wheel
  removed (pip bundles old urllib3/msgpack), `uv` image pinned. What remains has no Debian
  fix yet and sits in tools the app never runs; rebuilding regularly picks fixes up.
  Dockerfile configuration scan: clean.
- **OWASP ZAP** baseline (passive) and active API scan (from the OpenAPI spec, run with a
  dummy LLM key and demo login off so it couldn't spend tokens): first round 0 failures;
  warnings fixed (security headers, CSP without `'unsafe-inline'`, self-hosted font
  instead of Google Fonts, CORP/COEP, `no-store` on personal data). Second round: 0
  failures, only informational notes. The active scan sent 124 requests to the chat
  endpoint (all 401) and caused no server errors.
- **Manual review**: per-user scoping (tested), tokens from `secrets`, constant-time
  webhook HMAC, Argon2 passwords, hashed session tokens, CSRF origin check, rate limits,
  escaped statement page, API docs off by default (`ENABLE_API_DOCS`).
*Known gaps* (for paid tools / later): sign-up is open and was automated by ZAP (rate
limited to 5/min/IP; CAPTCHA or email verification before a public launch); no WAF; no
authenticated DAST of the chat (it costs model tokens); prompt-injection testing of the
agents is manual (money-moving is impossible by design, reminders need a confirmed second
turn, and tools only reach the user's own books).

To re-run: `uvx pip-audit`, `uvx bandit -r . -x ./.venv,./test`, Semgrep and Trivy via their
Docker images, and ZAP via `ghcr.io/zaproxy/zaproxy:stable` (`zap-baseline.py`,
`zap-api-scan.py` against `/openapi.json` with `ENABLE_API_DOCS=true`).

**D48. A second model provider: OpenRouter, same model.** Groq's paid tier couldn't be
bought ("Developer tier upgrades are temporarily unavailable due to high demand"), and its
free tier (200,000 tokens/day) covers about two full demo rehearsals. `gpt-oss-120b` is an
open-weight model hosted by many companies, so `LLM_MODEL=openrouter:openai/gpt-oss-120b`
(with `OPENROUTER_API_KEY`) runs the model we validated, at listed prices around $0.03-0.15
per million input tokens, with OpenRouter failing over between hosts. It speaks the OpenAI
API, so it uses `langchain-openai` with OpenRouter's address (`llm.py`). The curator asks for
JSON-schema output first and, if a host rejects that, retries once with tool calling.
Switching back to Groq is the same one-line setting.
*Trade-off:* a request may land on different hosts behind OpenRouter, whose speed and
quirks vary. Rehearse the demo on it before recording.

### 9.5 Error handling

| Where | Fails how | What happens |
|---|---|---|
| `LedgerEventBus.publish` | No running event loop | Logged (`ledger_event.no_loop`), skipped. Never raises: the ledger has already committed |
| `LedgerEventBus._run` | A handler raises anything | Logged with traceback (`ledger_event.handler_failed`); other handlers still run |
| `FalkorGraphMemory._client` | FalkorDB unreachable at first use | `graph.connect_failed`, raises `GraphMemoryError`; the next call tries again |
| `FalkorGraphMemory._run` | Redis/socket error, timeout | `graph.query_failed` (op, graph, error type), raises `GraphMemoryError` |
| `FalkorGraphMemory._ensure_schema` | Index/constraint already exists | Ignored (idempotent); any other error becomes `GraphMemoryError` |
| `GraphSync.on_ledger_changed` | `GraphMemoryError` | `graph_sync.failed` warning; the graph is behind until the next change or a rebuild |
| `main.lifespan` | FalkorDB down at startup | `graph_memory.unreachable_at_startup` error; the app starts anyway |
| `scripts/rebuild_graph.py` | One trader fails | Logged, reported, exit code 1; other traders continue. Safe to re-run |
| `GraphSync.on_ledger_changed` (retry) | Second failure | `graph_sync.failed attempt=2`; behind until next change or rebuild |
| `memory.recall.recall` | Graph unavailable | `recall.failed`; the agents run without memory (the reply still goes out) |
| `Curator._safe_curate` | Anything at all | `curator.crashed` with traceback; the reply was already sent |
| `Curator.curate` | Graph fails before writing | `curator.graph_failed`, one retry after 10 s (`curator.retrying`) |
| `Curator._propose` | Model error / bad output | `curator.model_failed` (error type, provider problem, detail) or `curator.model_bad_output`; no memory from that turn |
| `Curator._apply` | Invalid op, or graph error on one op | Rejected with a reason (`curator.done rejected=[…]`); other ops still apply |
| `extract_transactions_tool` | Ambiguous new name | Returns an error to the agent, which asks the user; nothing is recorded |
| `agents/team.py` node | Claimed recording, nothing recorded | `team.unbacked_claim`, one re-run; `team.unbacked_claim_again` if it happens twice |
| `agents/team.py` | Second handoff attempted | `team.handoff_refused`; the second agent's reply is used |
| `tools/turn_lock.user_turn_lock` | The same user's previous turn still running past `TURN_LOCK_TIMEOUT` | `turn_lock.busy` (process or database); `TurnInProgress`: "still working on your last message" |
| `jobs/worker.py` | A job's handler fails | `jobs.retry` with backoff; holds the user's later jobs; after `JOB_MAX_ATTEMPTS` → `jobs.failed`, parked (`scripts/jobs.py retry`) |
| `jobs/worker.py` | Unknown job kind | Failed at once (`Permanent`) |
| `jobs/worker.py` | Database or lock connection breaks | `jobs.worker_error`, back off, reconnect; jobs stay pending |
| `tools/turn_lock._advisory_lock` | Unlock fails | `turn_lock.unlock_failed`; closing the connection releases the lock anyway |
| `Collector.who_to_chase` | Sweep or recall fails | `collector.sweep_failed` / `collector.recall_failed`; ranks on the ledger alone (overdue, amount) |
| `Collector.do_not_remind` | Graph unavailable | `collector.check_failed`; returns no restriction (the prompt still carries what recall found, if anything) |
| `tools/alerts._graph_bank_name` | Graph unavailable | `alerts.graph_lookup_failed`; falls back to the PartyAlias table |
| `memory/explain.explain` | Graph unavailable / unknown person | `explain.failed` → "can't reach my memory"; unknown → "nothing remembered about X" |
| `Curator._apply_notes` | Note names a party the graph doesn't have | `curator.note_unknown_party`, skipped |
| `scripts/collector_sweep.py` | One trader fails | Logged, reported, exit 1; others continue. Safe to re-run |
| `FalkorGraphMemory._run` (reads) | Connection error / timeout | One immediate retry (`graph.query_failed attempt=1`), then `GraphMemoryError` |
| `tools/router.handle_message` | Malformed tool call (`tool_use_failed`) | `router.model_slip_retry`, one retry; again → `router.model_slip_again`, "say it another way" |
| `tools/router.handle_message` | Agent run longer than `LLM_TURN_TIMEOUT` | `router.turn_timeout`; "busy, try again"; counts towards the breaker |

### 9.6 Log events

Structured JSON via structlog (`utils.logger`).

| Event | Level | Meaning |
|---|---|---|
| `graph_memory.falkordb` / `graph_memory.off` | info | Adapter chosen at startup |
| `graph_memory.ready` / `graph_memory.unreachable_at_startup` | info / error | Startup ping result |
| `graph.connected` / `graph.connect_failed` | info / warning | Driver built, or not |
| `graph.schema_ready` | info | Indexes and constraints in place for a trader's graph |
| `graph.synced` | info | Parties, debts and stale removals for one sync |
| `graph_sync.done` / `graph_sync.failed` | info / warning | One ledger change mirrored (with ms), or not |
| `graph_sync.rebuilt`, `graph.pruned`, `graph.deleted` | info | Rebuild and maintenance |
| `graph.query` | debug | Every query, with op name and ms |
| `graph.query_failed` | warning | Failed query: op, graph, error type |
| `ledger_event.handler_failed` | error | A listener crashed (with traceback) |
| `recall.done` / `recall.failed` | info / warning | People found in a message (counts, ms), or graph unavailable |
| `team.routed` | info | Which agent got the message, and why |
| `team.agent_ran` | info | An agent finished (ms, and who it handed off to) |
| `team.handoff_refused` | warning | A second handoff was attempted |
| `team.unbacked_claim` / `team.unbacked_claim_again` | warning / error | Reply claimed a recording that didn't happen |
| `curator.done` | info | Promises, inferred orders, whether the model was asked, ops applied / unchanged / rejected (with reasons), ms |
| `curator.model_failed`, `curator.graph_failed`, `curator.retrying`, `curator.crashed` | warning / error | See 9.5 |
| `collector.swept` | info | Promises settled by a sweep (broken / kept counts) |
| `collector.ranked` | info | A ranking: debtors, listed, skipped, whether memory was used, ms |
| `explain.done` | info | A "why?" answer: promises, facts, aliases, quotes found |
| `router.model_slip_retry` / `router.model_slip_again` | warning / error | Malformed tool call retried / failed twice |
| `router.turn_timeout` | warning | An agent run hit the ceiling |
| `jobs.workers_started` / `jobs.workers_stopped` | info | Workers per process, Postgres or not |
| `jobs.done` | info | A job finished: kind, attempts, how long it waited in the queue, ms |
| `jobs.retry` / `jobs.failed` | warning / error | A job failed and will be retried / was parked |
| `turn_lock.waited` / `turn_lock.busy` | info / warning | A message waited over 1 s for the same user's previous one / gave up waiting |

### 9.7 The Cypher the product runs

All in `memory/falkor.py`:

| Constant | Does |
|---|---|
| `MERGE_TRADER` | The trader's own node, once per graph |
| `UPSERT_PARTY` | Create or rename a party |
| `UPSERT_DEBTS` | Create or update debts by Postgres id; move them if the party changed |
| `STATED_CUSTOMER_ROLES` | Has the trader ever stated this party's customer role? |
| `SET_INFERRED_CUSTOMER` / `DROP_INFERRED_CUSTOMER` | Customer role from the ledger |
| `REMOVE_STALE_DEBTS` | Drop these parties' debts the ledger no longer has, and their promises |
| `PRUNE_DEBTS` | Same, for the whole graph (rebuild) |
| `MARK_KEPT` | Open promises whose debt was cleared by the due date → `kept` (part of every sync) |
| `PARTY_NAMES`, `ALIAS_NAMES` | Every name the trader uses, for recall |
| `RECALL` | Roles, aliases, open debts, promises and facts for a set of parties |
| `CURRENT_FACT`, `DEBT_PARTY`, `OPEN_PROMISES` | Small lookups the curator validates against |
| `ADD_ALIAS` | Alias → party, with provenance |
| `REMOVE_EMPTY_PARTY` | Delete a party with nothing attached (a misheard name folded into an alias) |
| `ADD_PROMISE`, `SET_PROMISE_STATUS` | Promises and their outcomes |
| `ADD_FACT`, `CLOSE_FACT` | Usual orders and do-not-remind; a new fact closes the old one (`valid_to`) |
| `DELETE_INFERRED_ROLE`, `COUNT_CURRENT_STATED_ROLE`, `CLOSE_STATED_ROLE`, `CREATE_STATED_ROLE` | Stated roles, one constant per relationship type |
| `_SOURCE` (fragment) | `WRITTEN_BY` the agent and, if there's a message, `LEARNED_FROM` it |
| `OVERDUE_PROMISES` | Open promises past their due date, with their debt's state (the sweep) |
| `RECORD_REMINDER`, `LINK_REMINDER_REASONS` | A sent reminder, SENT_TO the party, BECAUSE its open/broken promises |
| `EXPLAIN_PROMISES`, `EXPLAIN_FACTS`, `EXPLAIN_ALIASES`, `EXPLAIN_ROLES`, `EXPLAIN_REMINDERS` | Everything about a party, with provenance, for "why?" |

### 9.8 Running and operating it

- `docker compose up` starts FalkorDB with the app. FalkorDB Browser:
  http://localhost:3000 (connect to host `localhost`, port `6379`). Each trader's graph is
  `trader_<user_id>`.
- Settings: `GRAPH_MEMORY` (`falkordb` / `off`), `FALKORDB_HOST`, `FALKORDB_PORT`,
  `FALKORDB_PASSWORD`, `FALKORDB_TIMEOUT_SECONDS` (default 5), `FALKORDB_BROWSER`,
  `CURATOR_MODEL`, `LLM_TURN_TIMEOUT` (default 90), `OPENROUTER_API_KEY` (for
  `LLM_MODEL=openrouter:…`), `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`
  (default 10 / 10 per process), `TURN_LOCK_TIMEOUT` (default 120).
- Scaling out: run more app processes freely. Turns stay one-at-a-time per user (D44);
  keep processes × (pool + overflow) under Postgres `max_connections` (D45). Graph sync
  and curation are durable jobs, shared across processes (D46); each worker holds one
  extra connection for its user locks.
- Job queue: `uv run python -m scripts.jobs status` (pending/failed by kind), `failed`,
  `retry [--id N]`, `run [--forever]` (drain the queue, or run a worker process).
  Settings: `JOB_QUEUE` (`durable` / `memory`), `JOB_WORKERS` (2), `JOB_MAX_ATTEMPTS` (6).
- Demo: `uv run python -m scripts.seed_demo [--reset]`, then `uv run python -m scripts.demo_check`.
- After an outage, or to backfill existing traders:
  `uv run python -m scripts.rebuild_graph --all` (or `--user 12`).
- Health: `GET /health` → `{"status": "ok", "graph": "ok"}`.
- Daily: `uv run python -m scripts.collector_sweep` (cron) settles overdue promises for
  every trader. Also runs on its own whenever a trader asks who to chase.

### 9.9 What the live runs found

Unit tests use scripted models. Before closing Day 2, the scenario's first messages were
run end to end against the real model (Groq `gpt-oss-120b`), FalkorDB and a throwaway
ledger, repeatedly, fixing what broke:

| Run | Found | Fix |
|---|---|---|
| 1 | Closed facts still showing as current; every open-debt count 0 | FalkorDB quirk (D27): filter in Python |
| 1 | Asking for a reminder made the curator lift "don't remind" | Curator rule: a request isn't permission |
| 1 | Curator calls failing (`commentary` tool, no tool) | JSON-schema structured output (D23) |
| 1 | The Recorder ignored the "ask which one" hint and created "Chidi's mum" as a new person | Ambiguous names refused in code (D24) |
| 2 | A real, different "Chidi" refused, and later merged into Mama Chidi | Weak resemblance is a hint only (D25); never absorb a name with ledger entries (D20) |
| 3 | The model confirmed a new name itself to get past the refusal | Confirmation only counts if the previous message asked (D24) |
| 3 | A 30-second stall in the graph connection lost one sync and one promise | One delayed retry for sync and curator (D3, D10) |
| 4 | "I don add sale: Pastor John" with nothing recorded | Unbacked-claim check (D26) |
| 5 | The curator learned an alias from the assistant's question, a turn early | Curator rule: only the trader's own words confirm |
| Day 3 | "Who I suppose chase?" → Emeka first ("promised by Sep 24", "broken 3 of 4"), Mama Chidi next, Pastor John skipped with the trader's words; "why Emeka first?" answered from the graph; "remind Pastor John" stopped by code | (worked as designed) |
| Day 3 | Groq's free tier: 200,000 tokens/day for `gpt-oss-120b`, used up by the day's live runs | Later checks on `gpt-oss-20b`; see limitations |
| Day 3 | `gpt-oss-20b` answered `[]` for "nothing to remember", rejected by Groq | D34 |
| Day 4 seed | The seeded day-30 graph matched the scenario exactly (Emeka broken/broken/kept/broken; Mama Chidi's three aliases and bank name; Bola's inferred usual order; Musa a supplier; Toyin broken at 67%; Chidi separate); day-1's ranking had Pastor John in it and no promise history | (worked as designed) |
| Day 4 rehearsal | An agent run took an hour, then every graph read timed out on dropped connections | D37, D39 |
| Day 4 rehearsal | `gpt-oss-20b` wrote unparseable tool calls, and those tripped the circuit breaker | D38 |
| Day 4 rehearsal | With recall down, "Chidi's mum" was filed as a new person (the name check needs recall) | D39 makes recall far less likely to fail; the gap is listed under limitations |
| Oct 16, free tier, full stack in Docker | Six demo messages on both accounts in ~32,000 tokens: day 30 ranked Emeka by broken promises and skipped Pastor John in the trader's words, offered "2 bags rice, ₦90,000, cash" for the usual, matched `OKAFOR CHINYERE MARY` to Mama Chidi, recorded "Chidi's mum" under Mama Chidi, and refused to remind Pastor John; day 1 did none of these | Recall now passes the usual amount and payment; "why … first/skipped" goes to the Collector, which explains with the ranking rule; Opik no longer breaks on an empty `OPIK_URL_OVERRIDE` from compose |

The final run: every claimed recording reached the ledger; "Chidi's mum" was asked about,
learned as Mama Chidi's alias, then recognised without asking; Chidi stayed a separate
person; "don't remind Pastor John" was remembered and the Collector checked before
drafting.

### 9.10 Known limitations

- Routing regexes are English/Pidgin-specific; unusual phrasings fall to the Recorder and
  rely on a handoff.
- The curator's judgement (when something is a stated alias, a usual order, a role
  change) is a model's: validated, logged and correctable, but not guaranteed. The
  scenario in `docs/hackathon-scenario.md` is the eval for it (Day 4).
- A memory missed because the graph was down for longer than one retry is not replayed;
  ledger structure comes back with a rebuild, learned memory from that turn doesn't.
- Recall's name matching is word-based: "Mama Nkechi" and "Nkechi's mum" are not linked
  unless the trader links them.
- The name check (D24) relies on recall. If the graph is unreachable for a message, a
  nickname can be recorded as a new person; `correct_transaction` plus an alias fixes it.
- Token budget: each message costs one agent run (often 2–3 model calls) plus, when worth
  it, one curator call. Groq's free tier allows 200,000 tokens a day per model, roughly
  100–150 messages. `CURATOR_MODEL=groq:openai/gpt-oss-20b` moves the curator onto a
  separate daily limit; a paid tier removes the ceiling.


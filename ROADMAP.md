# Book-Keeper Roadmap — making it stand out

Positioning: **the bookkeeper that gets Nigerian traders their money back.**
Talk to it the way you talk (Pidgin, Yoruba, Hausa, Igbo, voice notes on
WhatsApp); it keeps the books and chases the people who owe you.

The wedge is receivables ("who owe me?"). Everything below either makes that
wedge trustworthy, or takes it somewhere competitors (Kippa, Bumpa, OZE, POS
business accounts) don't go.

---

## Phase 1 — Trustworthy ledger (the numbers must be right)

A bookkeeper that gets the debt numbers wrong is worse than a notebook.

- [x] Money stored as `Numeric(18,2)`, never `Float`
- [x] Repayments settle the debts they belong to (FIFO by party), with an
      auditable `repayment_allocations` table
- [x] Credit works both ways: receivables (credit sales, loans given) and
      payables (credit purchases, loans taken)
- [x] P&L counts sales when made (accrual) and shows how much is still unpaid
- [x] Filling in a missing amount updates `amount_paid` / `outstanding` too
- [x] Undo ("cancel that") and corrections ("it was 45k not 50k") — soft
      deletes, repayment allocations reversed/reapplied
- [x] Transaction lookup actually filters by item/party; wording fits the type
- [x] System prompt: real disambiguation examples, incl. Pidgin and credit cases
- [x] Clarification cards actually reach the frontend
- [x] Ownership check on `/clarify`

## Phase 2 — Safe to put in front of real users

- [x] JWT auth on every bookkeeping endpoint (user from the token, never the body)
- [x] Working registration (business name, Nigerian phone normalized to +234), and
      login/refresh/logout actually mounted in the app
- [x] Web client: sign up / log in, silent token refresh, log out
- [x] Payment-system leftovers removed (transfer, balance, step-up, unused models,
      dead scripts and endpoints)
- [x] One LLM path chosen by config (`LLM_MODEL`); no key rotation; circuit breaker
      that actually trips on async failures
- [x] Rate limits per user (not per carrier IP)
- [x] CORS off unless `ALLOWED_ORIGINS` is set
- [x] `migrate.py` runs on deploy; Alembic actually installed and working
- [x] Docs rewritten; live eval script (`tools/evals.py`) with English + Pidgin cases

## Phase 3 — The differentiators

1. ✅ **WhatsApp-first channel** (Meta Cloud API webhook), with conversation memory
   shared across web and WhatsApp so follow-ups like "15k" work. Next: onboarding
   entirely inside WhatsApp (no web sign-up), and turning the web chat into a dashboard.
2. ✅ **Voice notes + local languages.** WhatsApp voice notes are transcribed in the
   language spoken (English, Pidgin, Yoruba, Hausa, Igbo) with an English translation
   where needed, echoed back ("🎤 what I heard"), then handled like typed messages.
   Next: voice in the web app (browser audio needs converting from webm), and
   Yoruba/Hausa/Igbo eval cases from real recordings.
3. **Debt collection loop.** Store debtor phone numbers; "remind Mama Chidi"
   sends a polite WhatsApp reminder with the balance; scheduled nudges on
   due dates ("she go pay Friday").
4. **Customer ledger statements.** Shareable per-customer statement
   ("see wetin you don buy and wetin you don pay").
5. **Bank / POS alert ingestion.** Forward a Moniepoint / OPay / bank alert
   and it's recorded (and matched to an open debt if possible).
6. **Credit profile.** Export 3–6 months of books as a lender-ready summary —
   path to partnerships with microfinance lenders.

## Phase 4 — Retention

- Daily/weekly WhatsApp summary ("You sell ₦240k this week, 3 people owe you ₦85k")
- Inventory-lite: stock bought vs sold for top items, cost of goods
- Offline/low-data tolerance (queue messages, short replies)

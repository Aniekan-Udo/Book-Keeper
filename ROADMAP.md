# Book-Keeper Roadmap — making it stand out

Positioning: **the bookkeeper that gets Nigerian traders their money back.**
Talk to it the way you talk (Pidgin, Yoruba, Hausa, Igbo, voice notes on
WhatsApp); it keeps the books and chases the people who owe you.

**Hard rule: the app never touches users' money.** It records, reports and reminds.
No payments, transfers, wallets, payment links or collecting on anyone's behalf —
every feature below stays on the right side of that line.

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

- [x] Auth on every bookkeeping endpoint (user from the session, never the body) —
      server-side sessions in an httpOnly cookie, with a CSRF origin check
- [x] Working registration (business name, Nigerian phone normalized to +234), and
      login/logout actually mounted in the app
- [x] Web client: sign up / log in, log out
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
   shared across web and WhatsApp so follow-ups like "15k" work.
   ✅ **Sign-up inside WhatsApp** (two questions, no email/password), passwordless web
   login and "Connect WhatsApp" by sending a code from the phone, and phone numbers
   only ever attached once proven. Next: turn the web chat into a dashboard; account
   deletion on request (NDPA).
2. ✅ **Voice notes + local languages.** WhatsApp voice notes are transcribed in the
   language spoken (English, Pidgin, Yoruba, Hausa, Igbo) with an English translation
   where needed, echoed back ("🎤 what I heard"), then handled like typed messages.
   Next: voice in the web app (browser audio needs converting from webm), and
   Yoruba/Hausa/Igbo eval cases from real recordings.
3. ✅ **Debt collection loop.** Debtor phone numbers, promised due dates and overdue
   tracking; "remind Mama Chidi" drafts a WhatsApp reminder the trader must confirm
   in a later message; opt-out (STOP), per-debtor cooldown and daily cap enforced
   server-side; debtor replies acknowledged and forwarded to the trader.
   Next: a daily "who's overdue" nudge to the trader (needs a second approved
   template + a scheduler), Pidgin-language reminder template, and eval cases for
   the confirm-before-send conversation.
4. ✅ **Customer ledger statements.** Statement of account per customer or supplier
   (running balance, due dates, brought-forward for a period), as forwardable text and
   a private, expiring, revocable printable link. No payment on it — a record only.
5. ✅ **Bank / POS alert ingestion.** Forwarded alert text or screenshots are parsed;
   clear repayments are recorded automatically, anything uncertain becomes a question;
   duplicates, re-forwarded alerts and fake "transfer successful" screens are caught;
   bank names are learned per customer. Read-only — never connects to accounts.
   Next: collect real alert samples from each major bank/fintech as eval cases.
6. **Credit profile.** Export 3–6 months of books as a lender-ready summary that
   the user shares with a lender themselves. We never lend, broker or move money.

## Phase 4 — Retention

- Daily/weekly WhatsApp summary ("You sell ₦240k this week, 3 people owe you ₦85k")
- Inventory-lite: stock bought vs sold for top items, cost of goods
- Offline/low-data tolerance (queue messages, short replies)

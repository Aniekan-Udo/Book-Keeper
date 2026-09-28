# Pilot runbook — 5 traders, 2 weeks

**Goal:** find out, with real traders, whether Book-Keeper records their business correctly
and whether they keep using it — before building anything else.

**Shape:** 5 traders · 2 weeks · WhatsApp (Meta's free test number) · you review every
mistake twice a week and fix it.

**We're done when we can answer:**
1. Accuracy — what share of messages needed a correction? (aim: under 10% by week 2)
2. Habit — how many traders used it on 3+ days a week, in week 2?
3. Value — did reminders or statements get anyone paid? Would they pay for it?

---

## Part A — Set up (you, ~1 day)

### 1. Model provider (Groq): billing in good standing
One trader message uses 2–3 model calls, plus Whisper for voice notes. Free tiers are
too small even for testing.

- [console.groq.com](https://console.groq.com) → Settings → Billing: a working payment
  method and nothing overdue. An overdue account is blocked outright.
- Set a spending limit / alert so there are no surprises. Check Groq's pricing page for
  current per-token and per-audio-minute rates; 5 traders will cost very little.
- If the provider refuses calls for billing reasons, traders get "busy, try again
  shortly" and the server log says `account credit is used up. Top up billing now.`
- Switching provider (e.g. to Gemini) is only `.env` changes — see `llm.py`.

### 2. A server with HTTPS
Meta only sends WhatsApp messages to an HTTPS address that's always on.

- Rent a small Linux VPS (1 vCPU, 2 GB RAM is plenty; any provider). Install Docker.
- Point a domain at it: a DNS **A record**, e.g. `books.yourdomain.com` → the server's IP.
- On the server:
  ```bash
  git clone <your repo> Book-Keeper && cd Book-Keeper
  git checkout roadmap
  cp .env.example .env && nano .env      # see the list below
  docker compose --profile production up -d --build
  curl https://books.yourdomain.com/health   # → {"status":"ok"}
  ```
- `.env` on the server needs at least:
  ```
  DOMAIN=books.yourdomain.com
  APP_PUBLIC_URL=https://books.yourdomain.com
  COOKIE_SECURE=true
  POSTGRES_USER=bookkeeper
  POSTGRES_PASSWORD=<long random>     # python -c "import secrets; print(secrets.token_urlsafe(24))"
  POSTGRES_DB=bookkeeper
  GROQ_API_KEY=<your key>          # models default to Groq, see .env.example
  PRIVACY_POLICY_URL=<step 5>
  # WHATSAPP_* from step 3
  ```
- Backups — traders' books live only in this database:
  ```bash
  crontab -e
  30 2 * * * cd ~/Book-Keeper && ./scripts/backup.sh >> backups/backup.log 2>&1
  ```
  Copy `backups/` off the server now and then (e.g. to your laptop or cloud storage).

### 3. Meta: a WhatsApp test number
1. [developers.facebook.com](https://developers.facebook.com) → Create app → type **Business** → add **WhatsApp**.
2. WhatsApp → API Setup: Meta gives you a **test phone number**. Note its **Phone number ID**.
3. Under "To", add each pilot trader's WhatsApp number (they each receive a code to confirm).
   Test numbers only work with a handful of pre-approved recipients — fine for 5 traders.
4. Business settings → System users → create one → generate a **permanent token** with
   `whatsapp_business_messaging` (the token on the API Setup page expires in 24 hours).
5. App settings → Basic → **App secret**.
6. WhatsApp → Configuration → Webhook:
   - Callback URL: `https://books.yourdomain.com/webhooks/whatsapp`
   - Verify token: any string you choose
   - Subscribe to: **messages**
7. Put these in the server's `.env`, then `docker compose --profile production up -d`:
   ```
   WHATSAPP_VERIFY_TOKEN=<the string from step 6>
   WHATSAPP_APP_SECRET=<step 5>
   WHATSAPP_ACCESS_TOKEN=<step 4>
   WHATSAPP_PHONE_NUMBER_ID=<step 2>
   WHATSAPP_DISPLAY_NUMBER=<the test number, digits only, e.g. 15551234567>
   ```
   Do step 6 after the server is running — Meta checks the URL when you save it.

### 4. (Optional for week 1) Reminder template
Payment reminders need an approved template — text in `Documentation.MD` → "Debt
collection". Submit it early; approval can take a day or more. Set
`WHATSAPP_REMINDER_TEMPLATE` once approved. Until then the bot says reminders are off.

### 5. Privacy notice
Required before collecting anyone's data (Nigeria Data Protection Act 2023). A starting
draft is in `docs/privacy-notice-draft.md` — fill in the blanks, have it reviewed, publish
it (any web page), and set `PRIVACY_POLICY_URL`.

### 6. Try it yourself first
From your own phone (add it as a test recipient too), message the test number:
1. "hi" → sign up (business name, your name)
2. "I sell 3 crates egg to Mama Chidi, 15k, she pay 10k, balance Friday. Her number na 0803…"
3. A voice note in Pidgin: "Emeka don pay 5k"
4. Forward a real credit alert from your bank
5. "who owe me?" · "send me Mama Chidi statement" · "cancel that"
6. On a laptop: `https://books.yourdomain.com` → Log in with WhatsApp

If any of these goes wrong, fix it before inviting traders.

---

## Part B — Recruit and onboard (week 0)

**Who (5 traders):** mix them so you learn fast —
- someone who sells a lot **on credit** (provisions, foodstuff) — tests debts and reminders
- a **market trader** who speaks mostly **Pidgin**
- someone who'd rather send **voice notes** (Yoruba, Hausa or Igbo if possible)
- a **POS agent** or someone paid mostly by **transfer** — tests bank alerts
- someone who already keeps a notebook or Excel — your benchmark

**Onboarding (10 minutes, in person or on a call):**
1. Add their number as a test recipient; they confirm Meta's code.
2. Tell them plainly:
   - "Just tell it what you sold, bought, borrowed or collected — like you'd tell a friend."
   - "Voice notes are fine. Forward your bank alerts to it."
   - "If it gets something wrong, just say so — 'no be 50k, na 45k' or 'cancel that'."
   - "It never touches your money. It only keeps your records."
   - **Consent:** "For these 2 weeks we'll read the messages to fix mistakes. You can stop
     any time." (Also covered in the privacy notice.)
3. They send "hi" and sign up while you watch. Do one real sale together.

---

## Part C — Run it (weeks 1–2)

**Daily (2 minutes):** is `/health` OK? Any trader stuck?

**Twice a week (1 hour) — the review loop:**
```bash
docker compose exec api uv run --no-sync python -m tools.pilot_report --days 4 --out /tmp/report.md
docker compose cp api:/tmp/report.md ./report.md      # read it, then delete it
```
For every exchange under **"To review"**:
1. What should the right result have been?
2. Add the trader's message to `tools/evals.py` as a case (anonymise names if you like).
3. Adjust the prompt (`tools/prompts.py`) until the eval passes, without breaking others:
   `uv run python -m tools.evals`
4. Deploy: `git pull && docker compose --profile production up -d --build`.

The report contains traders' messages and figures: keep it private, don't commit it.

**Weekly (10 minutes per trader) — ask:**
- What did you record this week? What didn't you bother recording, and why?
- Did it ever get something wrong? Did you notice?
- Did anyone pay you after a reminder or a statement?
- What do you wish it did?
- (End of week 2) Would you keep using it? Would you pay ₦___ a month for it?

---

## After two weeks

Look at the numbers in the last report plus what traders said:
- **Accuracy under ~10% and 3+ traders using it most days** → go wider: 20–50 traders on a
  real WhatsApp number (needs Meta business verification — start that during the pilot).
- **Accuracy is the problem** → another two weeks of the review loop before growing.
- **Nobody's using it** → the interviews will say why; that matters more than the code.

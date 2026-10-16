# Book-Keeper: a bookkeeper that remembers

A WhatsApp bookkeeping assistant for Nigerian market traders, with **graph memory on
FalkorDB** shared by a small team of agents. Traders type (or voice-note) what happened in
English or Pidgin, *"Emeka collect 2 bags beans, 60k. He go pay Monday"*, and it keeps the
books, chases debts and answers questions.

**Graph Hacks (WeMakeDevs × FalkorDB), Track 2: Agent Memory and Coordination.**

> Book-Keeper only keeps records, reports and drafts reminders. It never receives, sends or
> holds money.

---

## What graph memory changes

The same ledger, the same messages, two accounts. **Day 1** has nothing remembered;
**day 30** has a month of memory (`scripts/seed_demo.py` builds both from
[the demo scenario](docs/hackathon-scenario.md)).

| The trader says | Day 1 | Day 30 |
|---|---|---|
| "Chidi's mum pay 30k" | Has to ask who that is (and might file it under Chidi, the porter) | Recorded against **Mama Chidi**: it learned the nickname when she confirmed it |
| "Who I suppose chase?" | A list by amount, Pastor John included | **Emeka first**: "promised to pay by Sep 24 and hasn't", "has broken 3 of 4 promises". **Pastor John skipped**: "you said on Sep 8 not to remind them" |
| "Why Emeka first?" | No history to give | Each promise, its outcome, who decided it, and the trader's own message it came from |
| "Bola come, I give am the usual" | "What did Bola buy?" | "2 bags rice, ₦90,000, paid cash?" (learned from three identical sales) |
| Forwards an alert from `OKAFOR CHINYERE MARY` | "Whose payment is this?" | Mama Chidi's payment: the bank name was confirmed once, on Sep 4 |
| "Remind Pastor John" | Goes straight to drafting | Refused in code until the trader confirms: they said not to |

## How it works

```
 trader message ──► recall ──► route ──► agent team (LangGraph) ──► reply
                    (code)    (code)     Recorder │ Collector │ Alert matcher
                      ▲                       │ ledger tools
                      │                       ▼
                      │              Postgres (the money)
                      │                       │ after commit: LedgerChanged
                      │                       ▼
                   FalkorDB ◄────────── graph sync (code)
               one graph per trader ◄── memory curator (background, after the reply)
                                         model proposes → code checks → fixed Cypher writes
```

- **Postgres keeps the money; FalkorDB keeps the memory.** Balances never leave Postgres.
  The graph holds people, debts (by Postgres id), promises and their outcomes, nicknames
  and bank names, usual orders, do-not-remind, roles, and reminders.
- **Four agents, coordinating through the graph, never by message.**
  - The **Recorder** keeps the books.
  - The **Collector** decides who to chase and drafts reminders.
  - The **Alert matcher** reads forwarded bank alerts.
  - The **Memory curator** decides what to remember after each reply.

  What one writes, the others read on the next message.
- **Every memory has provenance:** `LEARNED_FROM` the trader's message and `WRITTEN_BY` the
  agent. That's what answers "why?".
- **The model proposes, code disposes.** The curator's model returns typed operations;
  code validates each one and runs fixed, parameterised Cypher. The model never writes
  Cypher. What can be computed is computed:
  - promises come from due dates
  - "kept" from the repayment date, "broken" from the Collector's sweep
  - the chase ranking is a sort over facts

Without FalkorDB the bookkeeping still works, but recall, nicknames, promise history,
ranking by reliability, do-not-remind, bank-name matching and "why?" all stop. They're
built on the graph.

## The web app

- **`/`**: the landing page. Keeping the books by chatting, what it tells you about your
  money, how its memory works, the trust rules, and the demo buttons.
- **`/app`**: the product. Same agents and memory as WhatsApp.
  - **Books** (the home screen): money made, expenses and profit for today, this week,
    this month, last month or all time; what sold and where the money went, by item;
    who owes you and who you owe; recent entries.
  - **Chat:** record anything or ask anything. History is kept across reloads, replies are
    formatted, there are amount prompts and suggested messages.
  - **Owed & owing:** every open balance, per person: what it was for, the amount, what's
    been paid, the balance, and the due date, with overdue flagged. One click to send a
    statement, draft a reminder or record a payment.
  - **People:** customers and suppliers, with balances and what's remembered about each
    (nicknames, promise record, usual order, do-not-remind). Open someone to see each
    memory with the message it came from and which agent saved it.
  - **Who to chase:** the Collector's ranking with its reasons, and who was skipped and why.

Every page except Chat is plain code over Postgres and the graph, with no model calls, so
it costs no tokens to explore. Login is by email or WhatsApp code; the demo buttons appear
only when the server sets `DEMO_ACCOUNTS=true`.

## Graph data model

One graph per trader (`trader_<user_id>`):

```
(:Trader {id})
(:Party {key, name})
(:Debt {pg_id, kind, open, date, due, settled_on})        -[:WITH]->        (:Party)
(:Party) -[:CUSTOMER_OF|SUPPLIER_OF {confidence, valid_from, valid_to}]->  (:Trader)
(:Alias {key, text, kind})                                 -[:REFERS_TO {valid_from, valid_to}]-> (:Party)
(:Promise {id, due, status, paid_fraction, closed_by})    -[:FOR]-> (:Debt), -[:MADE_BY]-> (:Party)
(:Fact {kind, summary, said, data, until, confidence, valid_from, valid_to}) -[:ABOUT]-> (:Party)
(:Reminder {pg_id, sent_at})                               -[:SENT_TO]-> (:Party), -[:BECAUSE]-> (:Promise)

(:Alias|:Promise|:Fact|:Reminder) -[:LEARNED_FROM]-> (:Message {turn_id})
(:Alias|:Promise|:Fact|:Reminder) -[:WRITTEN_BY]->   (:Agent {name})
```

| Memory | Comes from | Used by |
|---|---|---|
| Alias (nickname, misheard name, bank name) | Curator (trader confirms); Alert matcher (bank name confirmed) | Recall, the name check, alert matching |
| Promise: open / kept / broken / replaced | Code: a due date on an entry; kept by the sync, broken by the Collector | Collector ranking, "why?" |
| Usual order | Curator (trader says so), or code (3 identical sales) | Recorder ("the usual") |
| Do-not-remind | Curator (trader says so) | Collector: ranking and the reminder tool |
| Role (customer / supplier) | Code (first sale), Curator (trader says so) | Recorder, Collector |
| Reminder | Collector, when one is sent | "Why?" |

Facts are never deleted to correct them. They're closed (`valid_to`), so history stays
true: "Musa *was* a customer".

## The Cypher it runs

Every statement is a named, parameterised constant in [`memory/falkor.py`](memory/falkor.py)
(full table in [Documentation.MD §9.7](Documentation.MD)). A few that do the work:

```cypher
// A promise is kept when its debt was cleared on or before the due date (every sync)
MATCH (pr:Promise {status: 'open'})-[:FOR]->(d:Debt {open: false})-[:WITH]->(p:Party)
WHERE p.key IN $keys AND d.settled_on IS NOT NULL AND pr.due >= d.settled_on
SET pr.status = 'kept', pr.paid_fraction = 1.0, pr.closed_on = d.settled_on, pr.closed_by = 'ledger_sync'

// A nickname, with where it came from
MATCH (p:Party {key: $party_key})
MERGE (x:Alias {key: $alias_key}) ON CREATE SET x.text = $alias_text, x.kind = $kind
CREATE (x)-[:REFERS_TO {valid_from: $at}]->(p)
WITH x MERGE (ag:Agent {name: $agent}) MERGE (x)-[:WRITTEN_BY]->(ag)
WITH x FOREACH (_ IN CASE WHEN $turn_id IS NULL THEN [] ELSE [1] END |
  MERGE (m:Message {turn_id: $turn_id}) ON CREATE SET m.at = $at
  MERGE (x)-[:LEARNED_FROM]->(m))

// The Collector's sweep: open promises whose day has passed
MATCH (pr:Promise {status: 'open'})-[:FOR]->(d:Debt)
WHERE pr.due < $today
RETURN pr.id, d.pg_id, pr.due, d.open, d.settled_on

// "Why?": every promise, its outcome, who decided it and the message it came from
MATCH (pr:Promise)-[:MADE_BY]->(:Party {key: $key})
OPTIONAL MATCH (pr)-[:LEARNED_FROM]->(m:Message)
OPTIONAL MATCH (pr)-[:WRITTEN_BY]->(a:Agent)
OPTIONAL MATCH (pr)-[:FOR]->(d:Debt)
RETURN pr.due, pr.status, pr.paid_fraction, pr.made_on, pr.closed_on, pr.closed_by, m.turn_id, a.name, d.pg_id
ORDER BY pr.due
```

To look at a trader's memory in the FalkorDB Browser (http://localhost:3000, graph
`trader_<id>`):

```cypher
MATCH (p:Party {key: 'emeka'})<-[r]-(x) RETURN p, r, x
MATCH (x)-[:LEARNED_FROM]->(m:Message), (x)-[:WRITTEN_BY]->(a:Agent) RETURN x, m, a
```

## Run it

Requirements: Docker, and a Groq API key (or another provider, see `llm.py`).

```bash
cp .env.example .env            # set GROQ_API_KEY, Postgres credentials
docker compose up --build       # Postgres, FalkorDB, the app → http://localhost:8001
```

Without Docker for the app: `docker compose up -d postgres falkordb`, set `POSTGRES_URI` and
`FALKORDB_HOST=127.0.0.1`, then `uv run python migrate.py` and `uv run uvicorn main:app --reload`.

### The demo

```bash
uv run python -m scripts.seed_demo      # the two accounts: ngozi.day30@example.com, ngozi.day1@example.com
uv run python -m scripts.demo_check     # same messages to both, replies side by side (real model calls)
```

`seed_demo` replays the 30-day scenario through the real ledger, sync, curator and
Collector, with no model calls, so it's free and repeatable. It prints the password for
both accounts. With `DEMO_ACCOUNTS=true`, the landing page (http://localhost:8001) has
one-click "Open the day-30 shop" / "Open the day-1 shop" buttons. Then send:

1. `who I suppose chase?`
2. `why Emeka first?`
3. `Bola come, I give am the usual`
4. `(forwarded alert) Credit Alert! Amt: NGN30,000.00 Desc: TRF FROM OKAFOR CHINYERE MARY`
   (a bank name with no word in common with "Mama Chidi": only memory can match it)
5. `Chidi's mum take 1 bag rice, 45k, she go pay Monday`
6. `remind Pastor John`

Then open `trader_<id>` in the FalkorDB Browser. Re-seed before recording:
`uv run python -m scripts.seed_demo --reset`.

### Operations

- `uv run python -m scripts.collector_sweep`: the Collector's daily run (cron).
- `uv run python -m scripts.rebuild_graph --all`: rebuild ledger structure in the graph
  (learned memory is kept). Rarely needed now: graph sync and curation are durable jobs
  that retry until the graph is back.
- `uv run python -m scripts.jobs status | failed | retry`: look after the job queue.
- `GET /health` → `{"status": "ok", "graph": "ok"}`.

## Tests

```bash
docker compose up -d falkordb   # the graph tests run against a real FalkorDB (skipped if none)
uv run pytest                   # 358 tests (+4 that need a Postgres URL), no model calls
```

The memory tests cover:
- recall's name matching
- the curator's validation and provenance
- promise outcomes
- the Collector's ranking
- the guards found in live runs

Live runs against the real model, and what they changed, are written up in
[Documentation.MD §9.9](Documentation.MD).

## Design notes

- [`Documentation.MD` §9](Documentation.MD): architecture, design patterns, **47 numbered
  decisions with their trade-offs**, error handling, log events, every Cypher statement,
  live-run findings, and known limitations.
- [`docs/hackathon-design.md`](docs/hackathon-design.md): the product design: problem,
  agents, memory types.
- [`docs/hackathon-scenario.md`](docs/hackathon-scenario.md): the 30-day demo scenario
  (synthetic data).

## Use of AI assistance

Disclosed as Rule 11 requires:
- **By the team:** the product, the problem, the track, the design (memory types, agents,
  the custom-graph-over-Graphiti decision) and the scenario were decided by the team.
- **By Claude Code (Anthropic):** the code and documentation were written with it, during
  the event.
- **Checked by the team:** every phase was reviewed and tested by the team. That covers the
  test suite, plus live runs against the real model and FalkorDB whose failures were fixed
  and are recorded in Documentation.MD §9.9.

## Security

Scanned with gitleaks, pip-audit, Bandit, Semgrep (OWASP Top 10), Trivy and OWASP ZAP
(baseline and active API scan): no failures, dependencies with no known CVEs, the container
runs unprivileged, and every response carries a strict Content-Security-Policy and the
usual security headers. Findings, fixes and known gaps: Documentation.MD §9, D47.

## Data

All demo data is made up (Rule 6). Logs carry ids and counts, never customer names.

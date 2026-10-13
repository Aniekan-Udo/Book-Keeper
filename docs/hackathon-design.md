# Graph Hacks (FalkorDB) — design notes

Hackathon: Graph Hacks: Context for AI Agents (WeMakeDevs × FalkorDB), Oct 15 12:01 AM IST –
Oct 18 11:59 PM IST, 2026. Rules: https://www.wemakedevs.org/hackathons/falkordb/rules

**Track:** Agent Memory and Coordination.

These are planning notes, written before the event (allowed by Rule 8). No FalkorDB code
is written until Oct 15.

## The problem

Today the agent remembers the last 8 turns from the past 30 minutes
(`tools/assistant.py`, `HISTORY_WINDOW` / `HISTORY_TURNS`), plus the ledger rows. It
doesn't learn anything about a trader's business over time:

| Trader says | Today | With memory |
|---|---|---|
| "Chidi's mum paid 5k" | May create a new party | Knows it's Mama Chidi, and that "MAMA CHIDI OKAFOR" on bank alerts is her too |
| "She go pay Friday" (3 weeks ago) | A `due_date` on one row | A promise with an outcome: "Emeka has broken 3 of 4 promises" |
| "Give Bola the usual" | No idea | Bola usually buys 2 bags of rice at ₦45k |
| "Don't remind Pastor John" | Forgotten by tomorrow | Remembered, and the Collector respects it |
| "Emeka is my supplier, not a customer" | Fixes one row | Fact updated everywhere; old fact closed, not deleted |

The pitch: on day 30 the trader explains far less than on day 1, and every remembered fact
can be traced back to the message it came from.

### Why it has to be a graph

- **Identity:** many aliases → one party is a natural graph shape, and an awkward text search.
- **Facts that change over time:** promises made → kept/broken; roles that change. Stored as
  relationships with dates.
- **Every fact has a source:** each fact links to the message it came from (`LEARNED_FROM`),
  so the agent can answer "why do you think that?" and the trader can correct it.
- **Tokens:** fetch only facts about the parties named in this message instead of a longer
  history.

## Agents (coordination through the graph)

Agents never message each other. They share state only through the graph.

| Agent | When it runs | Reads from graph | Writes to graph | Built from |
|---|---|---|---|---|
| **Recorder** (talks to the trader) | Every message | Facts about the people in this message, aliases, "the usual" | Nothing directly | `ledger.py`, `clean_version.py` |
| **Memory curator** | In the background after each turn | Existing facts, to merge or contradict them | Facts, promises, aliases, preferences, each with its source message | New |
| **Collector** | Daily, plus "who should I chase?" | Promise history, do-not-remind, opt-outs, past reminders | Reminders sent and the reasons for each ranking | `debt_collection.py` |
| **Alert matcher** | When a bank alert is forwarded | Aliases, open promises | New bank-name aliases; promise marked kept | `alerts.py` |

The curator runs off the request path so the Recorder stays fast and cheap.

Memory is five fixed types, not free text: `alias`, `promise`, `usual_order`,
`do_not_remind`, `role`. See "Memory types" below.

The Collector only ranks and drafts. Sending still needs the trader's confirmation, and
nothing ever moves money (see the project rule: record, report, remind only).

## Graph model (first draft)

```
// one graph per trader: trader_<user_id>
(:Trader)
(:Party {key, name})            -[:CUSTOMER_OF|SUPPLIER_OF {valid_from, valid_to, confidence}]-> (:Trader)
(:Alias {key, text, kind})      -[:REFERS_TO {valid_from, valid_to}]-> (:Party)
(:Debt {pg_id})                 -[:OWED_BY]-> (:Party)         // amounts stay in Postgres
(:Promise {due, status, paid_fraction, said}) -[:FOR]-> (:Debt), -[:MADE_BY]-> (:Party)
(:Fact {kind, ...per kind, confidence, valid_from, valid_to}) -[:ABOUT]-> (:Party)
(:Message {turn_id})            <-[:LEARNED_FROM]- (:Fact|:Promise|:Alias)
(:Agent {name})                 <-[:WRITTEN_BY]- (:Fact|:Promise|:Alias)
(:Reminder {pg_id})             -[:SENT_TO]-> (:Party), -[:BECAUSE]-> (:Promise|:Fact)
```

- `LEARNED_FROM`: answers "why do you think that?"
- `WRITTEN_BY`: shows which agent wrote what, which makes the coordination visible.
- `valid_to`: corrections close the old fact instead of deleting it.
- `BECAUSE`: every reminder can be traced to the promise or fact behind it.
- Promises: a partial payment by the due date counts as broken, with `paid_fraction` kept.
  The Collector marks overdue promises broken at the start of every run; kept is marked by
  whichever agent records the clearing payment.
- Aliases: ask the trader only when more than one party could match; otherwise link and
  record the source message so it can be undone.

Collector ranking, sketch:

```cypher
MATCH (p:Party)-[:CUSTOMER_OF]->(:Trader)
WHERE NOT EXISTS {
  MATCH (f:Fact {kind: 'do_not_remind'})-[:ABOUT]->(p) WHERE f.valid_to IS NULL
}
OPTIONAL MATCH (pr:Promise)-[:MADE_BY]->(p)
RETURN p.name,
       count(CASE WHEN pr.status = 'broken' THEN 1 END) AS broken,
       count(CASE WHEN pr.status = 'kept' THEN 1 END)   AS kept
ORDER BY broken DESC
```

## Memory types

### Rules for every type

- **One graph per trader** (`trader_<user_id>`). One trader's agents can never read another
  trader's memory, and queries don't need a trader filter. The STOP opt-out list stays in
  Postgres because it applies across all traders.
- **Every item links to its source** (`LEARNED_FROM` a message, or the ledger row it came
  from) and to the agent that wrote it (`WRITTEN_BY`).
- **Confidence is `stated` or `inferred`**, not a number. `stated` = the trader said it.
  `inferred` = worked out from a pattern. Inferred items are always confirmed with the trader
  before they change what gets recorded.
- **Correct vs forget.** "That's wrong" closes the item (`valid_to` = now) and keeps the
  history. "Forget what you know about X" deletes the items for real (third-party privacy).
- **The curator never writes Cypher.** It returns a list of operations (`add_alias`,
  `add_promise`, `set_usual_order`, `set_do_not_remind`, `lift_do_not_remind`, `set_role`,
  `close_role`, `correct`, `forget`). Code validates them and runs fixed queries.
- **Not stored:** anything outside these five types (family, health, gossip), balances or
  amounts owed (Postgres), phone numbers (Postgres `contacts`).
- **Derived, not stored:** patterns the graph can compute, e.g. "usually pays on Friday" is a
  query over kept promises and payment dates, not a fact the LLM writes.

### 1. `alias`: another name for a party

| | |
|---|---|
| **Shape** | `(:Alias {key, text, kind})-[:REFERS_TO {valid_from, valid_to}]->(:Party)`; `kind` = `nickname` / `bank_name` / `transcription` |
| **Written by** | Curator (nickname, transcription fix); Alert matcher (bank name, after the trader confirms who paid) |
| **When** | A new name is used for someone who already exists, or the trader confirms a bank name |
| **Rules** | `key` = lower-cased, trimmed, spaces collapsed. A key points to one current party per trader. Ask when more than one party could match; link silently when only one can. Never link a name that is already its own party (Chidi is not Mama Chidi) |
| **Read by** | Recorder (resolve names before recording), Alert matcher (match payer) |
| **Correction** | Close the `REFERS_TO` edge; if the right party is given, add a new edge |

Replaces today's `PartyAlias` table, which keeps working until the alert matcher moves over.

### 2. `promise`: a date someone said they'd pay

| | |
|---|---|
| **Shape** | `(:Promise {due, status, paid_fraction, said})-[:FOR]->(:Debt)`, `-[:MADE_BY]->(:Party)`; `status` = `open` / `kept` / `broken` / `replaced`; `said` = the words used ("he go pay Monday") |
| **Written by** | Curator creates; Alert matcher or curator marks `kept`; Collector marks `broken` |
| **When** | Only when the trader states a date for a debt. Never inferred. Relative dates ("Monday", "next week Tuesday") resolve against the message date |
| **Rules** | Kept = debt cleared by the due date. Broken = due date passed with a balance left (`paid_fraction` records how much was paid). A new date for the same debt creates a new promise: if the old one wasn't due yet it becomes `replaced` (doesn't count against them); if it was already overdue it stays `broken`. Also sets the ledger row's `due_date`, as today |
| **Read by** | Collector (ranking, reminders), "why?" |
| **Correction** | Close it (wrong date or wrong person) and create the right one |

### 3. `usual_order`: what "the usual" means for a customer

| | |
|---|---|
| **Shape** | `(:Fact {kind: 'usual_order', items: [{item, quantity, unit}], last_amount, payment: cash/credit})-[:ABOUT]->(:Party)` |
| **Written by** | Curator |
| **When** | `stated`: the trader says what the usual is. `inferred`: the same items and quantities three sales in a row, counted by a query, not judged by the LLM |
| **Rules** | One current usual order per party; a new one closes the old. "The usual" is **always confirmed** before recording ("2 bags rice, ₦90,000, cash. Record it?"), since prices change. `last_amount` is only a suggestion |
| **Read by** | Recorder |
| **Correction** | Close it; the next pattern of three (or a statement) sets a new one |

### 4. `do_not_remind`: the trader doesn't want someone chased

| | |
|---|---|
| **Shape** | `(:Fact {kind: 'do_not_remind', said, until})-[:ABOUT]->(:Party)`; `until` is optional ("no remind am till month end") |
| **Written by** | Curator |
| **When** | Only when the trader says so. Never inferred |
| **Rules** | Collector leaves them off the chase list and says why. If the trader asks to remind them anyway, ask once: "You said on Sep 8 you'd talk to him yourself. Still send?" Separate from STOP opt-outs, which come from the customer, apply across traders, and live in Postgres |
| **Read by** | Collector |
| **Lifting** | "You fit remind Pastor John now", or `until` passes: `valid_to` set |

### 5. `role`: customer, supplier, or both

| | |
|---|---|
| **Shape** | `(:Party)-[:CUSTOMER_OF / :SUPPLIER_OF {valid_from, valid_to, confidence}]->(:Trader)` |
| **Written by** | Sync code (`inferred` customer from a sale or loan given); curator (`stated`, from what the trader says) |
| **When** | First sale to someone: customer. Supplier only when the trader says so or clearly buys stock from them ("I buy 10 tins from Musa"). Paying for labour (Chidi carrying bags) gives no role |
| **Rules** | Both roles can be current at once. A statement ("Musa no dey buy from me again") closes a role; a stated role is never overwritten by an inferred one |
| **Read by** | Recorder (is "Musa bring 10 tins" a purchase or a sale?), Collector (wording; suppliers aren't chased) |
| **Correction** | Close the wrong role, open the right one |

## Orchestration (LangGraph + FalkorDB)

`langchain.agents.create_agent` (used in `tools/router.py`) already returns a LangGraph
graph, so this uses more of LangGraph rather than adding a framework.

- **LangGraph controls the flow within one message:** which agent runs, in what order.
- **FalkorDB is the memory across messages and across agents.** LangGraph state disappears
  after each run; the graph is what lasts. Agents never message each other.

```
WhatsApp/web message
      │
  route (plain code, no LLM)
      ├── forwarded bank alert ─────────► Alert matcher agent ──┐
      └── anything else ─► Recorder agent ──┬───────────────────┤──► reply
                                            └─"who I suppose chase?"
                                               ─► Collector agent ┘
                    (after the reply, off the request path)
                         └─► Memory curator agent ─► writes facts to FalkorDB

  Daily schedule ─► Collector agent (marks promises broken, ranks debtors)
```

- **Routing is plain code**, not a supervisor LLM: a forwarded alert is easy to detect, and
  skipping a model call keeps the token savings.
- **Recorder → Collector handoff** is the only routing the model decides, via a handoff tool.
- **The curator runs after the reply is sent** (background task), so the trader never waits
  on it.
- **Each agent** is its own `create_agent` with a short prompt and only its own tools, so no
  agent carries every tool description.

### How agents reach FalkorDB

**Fixed, parameterized Cypher behind ordinary tools**, using the async `falkordb` Python
client, with the graph connection passed in alongside the Postgres session the way
`build_tools(db, user_id, ...)` works today. First Day 1 task: confirm the client's asyncio
support (`falkordb.asyncio`).

Considered and not used as the core:

| Option | Why not |
|---|---|
| `langchain-falkordb` `FalkorDBGraph` | Docs don't mention async; the app is fully async |
| `langchain-falkordb` `FalkorDBSaver` (checkpointer) | Optional extra. Saves per-run state, not the memory being judged |
| LLM-writes-Cypher (the pattern in FalkorDB's LangGraph docs) | Unpredictable queries; their examples build Cypher from strings; Rule 12 needs explainable queries |
| Graphiti (`graphiti-core[falkordb]`) | Docs show OpenAI only (we use Groq); free-form extraction loses the fixed fact kinds and promise/`WRITTEN_BY` design; weakens the "new FalkorDB work" case |

## Keeping the graph honest

- **Postgres stays the source of truth for money.** The graph stores Postgres ids, never
  balances.
- **Ordering:** write Postgres, commit, then update the graph. A failed graph write is
  logged, not fatal to the ledger.
- **`rebuild_graph` script:** recreates Party/Debt/Promise structure from Postgres. Facts
  learned by the curator can't be rebuilt from Postgres, and that's acceptable.
- **Rule 3 (graph must do real work):** Collector ranking, alias resolution, "the usual" and
  "why?" all read from the graph. Without FalkorDB those features stop working.

## Risks

- **Wrong memories:** every fact carries a source message and a confidence. The trader can
  say "that's wrong" or "forget that".
- **Facts about third parties (customers):** demo uses synthetic data only (Rule 6), and the
  trader can delete what's stored.
- **Day 2 (fact extraction on Groq models)** is where time will go. Hence the fixed list of
  fact kinds.

## Plan

**Before Oct 15 (planning only):**
- Register on WeMakeDevs.
- Organizers contacted about building on the existing repo.
- Finalise the graph model.
- Write the demo scenario: one made-up trader, ~6 customers, ~30 days of messages.
- Check that FalkorDB runs in Docker.

**Day 1 (Oct 15):** Confirm async `falkordb` client; FalkorDB in `docker-compose`; graph client and schema; Postgres → graph
sync for parties, debts and promises; `rebuild_graph` script.

**Day 2 (Oct 16):** Split the single agent: code router + Recorder with its own tools;
Memory curator as a background task (facts, aliases, promises with sources; corrections);
Recorder fetches graph context into its prompt.

**Day 3 (Oct 17):** Collector ranking with reasons; Alert matcher on graph aliases, promises
kept/broken; "why?" tool.

**Day 4 (Oct 18):** Demo seed script; README with graph model and Cypher queries (Rule 10);
deploy; demo video; blog post for the side prize if time allows.

## Submission checklist (Rule 10)

- [ ] Public repo
- [ ] README: setup, graph data model, the Cypher queries the product depends on
- [ ] Demo video
- [ ] Live deployment, or full local setup instructions
- [ ] Track named: Agent Memory and Coordination
- [ ] AI coding assistant use disclosed (Rule 11)

## Demo story

Full scenario, timeline and test cases: `docs/hackathon-scenario.md`.

Same trader, "day 1" vs "day 30". On day 30: "Chidi's mum paid" resolves correctly; the
Collector skips Pastor John and puts Emeka (broken promises) first; the trader asks "why?"
and the agent shows the path through the graph, including which agent wrote each fact.

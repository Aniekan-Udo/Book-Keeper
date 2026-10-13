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

Fact kinds are a fixed list, not free text: `alias`, `promise`, `usual_order`,
`do_not_remind`, `role`.

The Collector only ranks and drafts. Sending still needs the trader's confirmation, and
nothing ever moves money (see the project rule: record, report, remind only).

## Graph model (first draft)

```
(:Trader {id})
(:Party {key, name})            -[:CUSTOMER_OF|SUPPLIER_OF]-> (:Trader)
(:Alias {text})                 -[:REFERS_TO]-> (:Party)       // "Chidi's mum", "MAMA CHIDI OKAFOR"
(:Debt {pg_id})                 -[:OWED_BY]-> (:Party)         // amounts stay in Postgres
(:Promise {due, status: open|kept|broken, paid_fraction}) -[:FOR]-> (:Debt), -[:MADE_BY]-> (:Party)
(:Fact {text, kind, confidence, valid_from, valid_to}) -[:ABOUT]-> (:Party|:Trader)
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
MATCH (p:Party)-[:CUSTOMER_OF]->(:Trader {id: $t})
WHERE NOT EXISTS {
  MATCH (f:Fact {kind: 'do_not_remind'})-[:ABOUT]->(p) WHERE f.valid_to IS NULL
}
OPTIONAL MATCH (pr:Promise)-[:MADE_BY]->(p)
RETURN p.name,
       count(CASE WHEN pr.status = 'broken' THEN 1 END) AS broken,
       count(CASE WHEN pr.status = 'kept' THEN 1 END)   AS kept
ORDER BY broken DESC
```

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

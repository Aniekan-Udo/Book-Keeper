# Teaching a market bookkeeper to remember: graph memory with FalkorDB

*Draft for the Graph Hacks blog prize. Edit freely before publishing.*

Ngozi sells rice, beans, garri and palm oil from a stall in a Nigerian market. She keeps her
books by sending WhatsApp messages to Book-Keeper, an AI assistant: *"Emeka collect 2 bags
beans, 60k. He go pay Monday."* It records the sale, tracks who owes what, and drafts polite
payment reminders.

For a long time it had the memory of a goldfish: the last eight messages, for thirty
minutes. After that, every conversation started from zero. It didn't know that "Chidi's
mum" is Mama Chidi, that Emeka has broken three of his four promises to pay, or that Ngozi
had asked it never to chase Pastor John. A human bookkeeper learns those things in a week.
For Graph Hacks, I taught mine.

## Why a graph, and not a vector store

Most of what a trader's assistant needs to remember isn't "text similar to this text".
It's:

- **Who is who.** "Chidi's mum", "Mama C" and the bank name `OKAFOR CHINYERE MARY` are one
  person. Chidi, who carries bags for ₦2,000, is someone else.
- **What happened, and how it turned out.** Emeka promised Monday, asked for Friday,
  paid late, then promised Thursday and didn't pay.
- **What changed.** Alhaji Musa used to buy garri; now he supplies palm oil.
- **Where each belief came from**, so the trader can ask "why?" and correct it.

Those are nodes, relationships and timestamps. So each trader gets their own FalkorDB
graph: parties, debts (pointing at the Postgres ledger, never copying amounts), promises
with outcomes, aliases, facts, roles and reminders. Every memory links to the message it
was learned from (`LEARNED_FROM`) and the agent that wrote it (`WRITTEN_BY`).

## Four agents that never talk to each other

The single do-everything agent became a small team, run as a LangGraph state graph:

- the **Recorder** keeps the books
- the **Collector** decides who to chase
- the **Alert matcher** reads forwarded bank alerts
- the **Memory curator** runs after every reply and decides what to remember

They don't pass messages. They coordinate **through the graph**:

- The curator turns a due date into a promise.
- The ledger sync marks it kept when the money arrives on time.
- The Collector marks it broken when it doesn't.
- Next week the Collector ranks Emeka first, with the reasons, because of what the others
  wrote.

When Ngozi says "no remind Pastor John o, I go talk to am myself", the curator stores
that with her exact words. Days later the Collector skips him: "you said on Sep 8 not to
remind them". If she asks to remind him anyway, the reminder tool itself refuses until
she confirms.

## The model proposes, code disposes

The biggest design choice: **the model never writes to the graph.** The curator's model
fills in a small typed form ("add alias: Chidi's mum → Mama Chidi"). Code checks each
proposal:
- Does the person exist?
- Was that name actually said?
- Is the "alias" really a separate person with their own records?

Only then does a fixed, parameterised Cypher statement write it.

And anything that can be computed isn't left to the model at all:
- A due date *is* a promise.
- Clearing the debt by that date *is* keeping it.
- Three identical sales in a row *are* a usual order.
- "Who to chase" is a sort over broken promises, days overdue and amount, so the answer to
  "why Emeka first?" matches the ranking exactly.

I considered Graphiti, which runs nicely on FalkorDB and Groq, and kept it for a later
"tell me about…" recall layer. For Book-Keeper's core, memory drives decisions, and
decisions need exact state, not similar-sounding sentences.

## What the live runs taught me

Unit tests with scripted models all passed. Then I ran the scenario against the real
model, and it broke in instructive ways:

- **It claimed work it didn't do.** "I don add sale: Pastor John…", with nothing in the
  ledger. Now, if a reply claims a recording and no ledger tool ran, the agent is re-run
  and told so.
- **It split and merged customers.** It recorded "Chidi's mum" as a brand-new person. When
  I hinted harder, it filed Chidi's ₦2,000 under Mama Chidi instead. The fix was in code:
  a new name that *strongly* resembles someone known (a possessive, a short form) is
  refused until the trader says who they mean. A single shared word is only a hint, and
  the hint says "a different person". The model even learned to "confirm" a name itself to
  get past the refusal, so a confirmation now only counts if the previous message asked.
- **The graph engine had opinions.** In FalkorDB, after a `WITH … collect()`, an
  `OPTIONAL MATCH` with a property filter matched nothing. Every open-debt count came back
  zero until I filtered in Python instead.
- **Infrastructure is part of memory.** Idle connections died quietly, and a provider
  slowdown could hold a trader's message for minutes. Keepalive, health checks, read-only
  queries with a retry, and a hard ceiling on each agent run fixed that.

Each of these is a numbered decision, with its trade-off, in the project's documentation.

## Day 1 vs day 30

The demo seeds two accounts with the same ledger. One has a month of memory; the other
has none. Same messages to both:

| | Day 1 | Day 30 |
|---|---|---|
| "Chidi's mum pay 30k" | "Who is that?" | Recorded against Mama Chidi |
| "Who I suppose chase?" | By amount | Emeka first (broken 3 of 4); Pastor John skipped, in her words |
| "Why Emeka first?" | Nothing to say | Each promise, its outcome, and the message it came from |
| An alert from `NWOSU EMEKA CHUKWUDI` | "Whose payment?" | Emeka's |

That table is the whole pitch: an assistant that, after a month, needs far less
explaining, and can always show its working.

*Built with FalkorDB, LangGraph, Postgres, FastAPI and Groq (`gpt-oss`), with Claude Code as
a coding assistant. All demo data is made up.*

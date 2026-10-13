# Demo scenario — 30 days of a made-up trader

Synthetic data for the Graph Hacks demo (Rule 6: no real traders or customers). This is the
source for the Day 4 seed script and the Day 2 curator test cases. Design:
`docs/hackathon-design.md`.

## The trader

**Ngozi Eze**, *Ngozi Provisions*: a foodstuff stall selling rice, beans, garri and palm oil.
Talks to Book-Keeper on WhatsApp in a mix of English and Pidgin, sometimes by voice note.

Prices used throughout: rice ₦45,000/bag, beans ₦30,000/bag, garri ₦15,000/bag,
palm oil ₦12,000/tin.

## The people

| Party | Role | What the scenario tests |
|---|---|---|
| **Mama Chidi** (Chinyere Okafor) | Customer | Aliases: "Chidi's mum", "Mama C", bank name `OKAFOR CHINYERE MARY`. Reliable, pays on Fridays |
| **Emeka** (Emeka Nwosu) | Customer | Breaks promises: 3 of 4 broken. Bank name `NWOSU EMEKA CHUKWUDI` |
| **Bola** | Customer | "The usual" = 2 bags rice, cash. Voice note mis-transcribed as "Bolu" |
| **Pastor John** | Customer | Do-not-remind |
| **Alhaji Musa** | Customer → **supplier** | Role change. Ngozi ends up owing him |
| **Iya Toyin** | Customer | Partial payment by transfer, bank name `ADEBAYO TOYIN F` |
| **Chidi** | Porter (paid for labour) | Must **not** be merged with Mama Chidi |

## Timeline (September 2026)

Columns: what Ngozi sends → what the ledger records (Postgres) → what goes into the graph,
and which agent writes it.

| Day | Message | Ledger | Graph (agent) |
|---|---|---|---|
| Wed 2 | "Emeka collect 2 bags beans, 60k. He go pay Monday" | Credit sale ₦60,000, due Sep 7 | Promise #E1 due Sep 7 (curator) |
| Thu 3 | "Mama Chidi take 1 bag rice on credit, she go pay Friday" | Credit sale ₦45,000, due Sep 4 | Promise #M1 due Sep 4 (curator) |
| Fri 4 | *Forwards alert:* credit ₦45,000 from `OKAFOR CHINYERE MARY` | Repayment ₦45,000 → Mama Chidi | Ngozi confirms who → alias `OKAFOR CHINYERE MARY`; #M1 **kept** (alert matcher) |
| Sat 5 | "Bola buy 2 bags rice, 90k cash" | Cash sale ₦90,000 | — |
| Sun 6 | "Pastor John take 1 bag beans for church program, 30k, e never pay" | Credit sale ₦30,000 | — |
| Mon 7 | "Alhaji Musa buy 5 bags garri, 75k, credit" | Credit sale ₦75,000 | — |
| Tue 8 | "No remind Pastor John o, I go talk to am myself" | — | Fact `do_not_remind` → Pastor John (curator) |
| Tue 8 | "Iya Toyin collect 4 bags garri, 60k. She say next week Tuesday" | Credit sale ₦60,000, due Sep 15 | Promise #T1 due Sep 15 (curator) |
| Tue 8 | *(Collector daily run)* | — | #E1 **broken**: Sep 7 passed with nothing paid (collector) |
| Wed 9 | "Emeka beg make I give am till Friday" | Due date → Sep 11 | Promise #E2 due Sep 11 (curator) |
| Wed 9 | "Alhaji Musa don pay the 75k cash" | Repayment ₦75,000 | — |
| Thu 10 | "Chidi's mum take 2 bags garri, 30k, Friday" | Credit sale ₦30,000 → **Mama Chidi** | Recorder asks once: "Is Chidi's mum the same as Mama Chidi?" → yes → alias `Chidi's mum`; promise #M2 due Sep 11 (curator) |
| Fri 11 | *Forwards alert:* credit ₦30,000 from `OKAFOR CHINYERE MARY` | Repayment ₦30,000 → Mama Chidi | **No question asked**: matched by alias; #M2 **kept** (alert matcher) |
| Sat 12 | "Bola buy 2 bags rice again, cash" | Cash sale ₦90,000 | — |
| Sat 12 | "I pay Chidi 2k to carry the bags" | Expense ₦2,000 → Chidi | New party **Chidi**, not linked to Mama Chidi |
| Sat 12 | *(Collector daily run)* | — | #E2 **broken** (collector) |
| Mon 14 | *Forwards alert:* credit ₦60,000 from `NWOSU EMEKA CHUKWUDI` | Repayment ₦60,000 → Emeka | Ngozi confirms → alias `NWOSU EMEKA CHUKWUDI` (alert matcher). Debt cleared; #E2 stays broken (late) |
| Tue 15 | *Forwards alert:* credit ₦40,000 from `ADEBAYO TOYIN F` | Repayment ₦40,000 → Iya Toyin, ₦20,000 left | Ngozi confirms → alias `ADEBAYO TOYIN F`; #T1 **broken**, `paid_fraction` 0.67 (alert matcher) |
| Tue 15 | "Emeka take 1 bag rice, 45k, Friday" | Credit sale ₦45,000, due Sep 18 | Promise #E3 due Sep 18 (curator) |
| Wed 16 | "Alhaji Musa no dey buy from me again, na him dey supply my palm oil now. I buy 10 tins, 120k, pay am 70k" | Expense ₦120,000, paid ₦70,000, owed ₦50,000 | Role `customer` closed, role `supplier` opened for Musa (curator) |
| Fri 18 | "Emeka pay the 45k cash" | Repayment ₦45,000 | #E3 **kept** (curator) |
| Sat 19 | *Voice note:* "Bola buy 2 bags rice, cash" → transcribed **"Bolu"** | Cash sale ₦90,000 → "Bolu" | New party Bolu (curator), a **wrong memory** |
| Sat 19 | "Bolu na Bola, the voice thing spell am wrong" | Sale's party → Bola | Bolu merged into Bola as alias; source: this message (curator) |
| Sat 19 | *(curator, after the third identical order)* | — | `usual_order` → Bola: 2 bags rice, cash, `inferred`; sources: the Sep 5, 12, 19 sales |
| Mon 21 | "Emeka take 3 tins oil, 36k. He go pay Thursday" | Credit sale ₦36,000, due Sep 24 | Promise #E4 due Sep 24 (curator) |
| Tue 22 | "Iya Toyin bring the remaining 20k cash" | Repayment ₦20,000 | — |
| Thu 24 | "Mama C take 1 bag rice" | Credit sale ₦45,000 → **Mama Chidi** | Alias `Mama C` (curator). No promise stated |
| Fri 25 | *Forwards alert:* credit ₦45,000 from `OKAFOR CHINYERE MARY` | Repayment ₦45,000 | Matched by alias. Nothing new stored: "usually pays on Friday" is now visible to a query over her payments (Sep 4, 11, 25) |
| Fri 25 | *(Collector daily run)* | — | #E4 **broken** (collector) |
| Sat 26 | "Wetin you know about Bola?" | — | *Read only:* usual order, alias Bolu, each with its source message |
| Tue 29 | "Mama Chidi take 1 bag beans" | Credit sale ₦30,000 | — |

### Balances going into day 30

| Party | Owes Ngozi | Ngozi owes | Promise record |
|---|---|---|---|
| Emeka | ₦36,000 | — | 4 promises: 3 broken, 1 kept |
| Pastor John | ₦30,000 | — | Do-not-remind |
| Mama Chidi | ₦30,000 | — | 2 kept, pays Fridays |
| Alhaji Musa | — | ₦50,000 | Supplier now |
| Iya Toyin, Bola, Chidi | — | — | — |

## Day 30 demo (Wed Sep 30)

Same messages sent to two accounts: **Day 30** (seeded with the timeline above) and
**Day 1** (a new trader with the same ledger but an empty graph). The contrast is the demo.

1. **"Chidi's mum pay 30k"**
   - Day 30: recorded against Mama Chidi. Balance cleared.
   - Day 1: "Who is Chidi's mum?", or worse, matches Chidi the porter.
2. **"Who I suppose chase?"** (Collector)
   - Day 30:
     1. Emeka, ₦36,000: promised Sep 24 and didn't pay; has broken 3 of his 4 promises.
     - Skipped: Pastor John, ₦30,000. "You said on Sep 8 you'd talk to him yourself."
     - Not listed: Alhaji Musa, who's your supplier now; you owe him ₦50,000.
   - Day 1: a list sorted by amount, including Pastor John.
3. **"Why Emeka first?"**
   - Day 30: shows the four promises with dates, outcomes and the messages they came from.
     Each fact is labelled with the agent that wrote it.
4. **"Bola come, I give am the usual"**
   - Day 30: "2 bags rice, ₦90,000, paid cash. Record it?"
   - Day 1: "What did Bola buy?"
5. **Forwards alert:** credit ₦36,000 from `NWOSU EMEKA CHUKWUDI`
   - Day 30: matched to Emeka with no question asked. The chase list is now empty apart from
     Pastor John, who is skipped. #E4 stays broken because the payment was late.
   - Day 1: asks who sent it.

Close on the FalkorDB Browser showing the graph: Emeka's promise chain, Mama Chidi's three
aliases, Chidi the porter on his own, and the `WRITTEN_BY` edges to each agent.

## Coordination moments to point out

Each is one agent writing and a different agent reading:

| Written by | Read by | Moment |
|---|---|---|
| Curator: promise | Alert matcher → Collector | Sep 11: alert marks #M2 kept, so Mama Chidi drops off the chase list |
| Alert matcher: bank alias | Alert matcher, later | Sep 11, 25, Day 30: alerts matched without asking |
| Curator: `do_not_remind` | Collector | Day 30: Pastor John skipped, with the reason |
| Curator: role change | Collector, Recorder | Day 30: Musa not chased; his entries recorded as purchases |
| Collector: promise broken | Collector ranking, "why?" | Day 30: Emeka first |

## Curator test cases (Day 2)

Every timeline row with a curator write is a test: given the message, and the graph state
before it, the curator should write exactly that. Negative cases matter as much:

- Sep 12, "I pay Chidi 2k": must **not** create an alias to Mama Chidi.
- Sep 5 and 12, Bola's orders: no `usual_order` yet (wait for the third).
- Sep 26, "Wetin you know about Bola?": a question, so nothing is written.
- Sep 24, "Mama C take 1 bag rice": no promise was stated, so no promise is invented.

## Decisions

1. **Partial payment by the due date** (Iya Toyin, Sep 15): the promise stores
   `paid_fraction` and counts as **broken** for ranking. "Why?" can still say "paid two thirds
   on time".
2. **Who marks promises broken:** the Collector, at the start of **every** run (daily and
   on demand), marks any open promise past its due date with a balance left as broken. The
   status is stored in the graph, so ranking queries stay simple and "why?" can cite it, and it
   is never stale when the Collector reads it. Kept is marked by whoever records the payment
   that clears the debt by the due date (Alert matcher or curator).
3. **When to ask before linking an alias:** ask when there is more than one possible match
   ("Chidi's mum": Mama Chidi or Chidi), link silently when there is one ("Mama C"). Silent
   links still record their source message, so the trader can undo them.

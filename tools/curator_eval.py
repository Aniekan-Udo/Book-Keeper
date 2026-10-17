"""Live check of the memory curator's judgement: does the model propose the right memory
operations (and nothing else) for real trader messages?

Cases come from docs/hackathon-scenario.md, half of them traps (a single sale isn't a usual
order; asking for a reminder isn't permission; a porter called Chidi isn't Mama Chidi). This
scores what the model *proposes*; code still validates every proposal before anything is
written, so a wrong proposal here is caught later, but it's the model's accuracy we want.

Uses real model calls (CURATOR_MODEL, else LLM_MODEL), about 2,000 tokens per case:

    uv run python -m tools.curator_eval
    uv run python -m tools.curator_eval alias      # only cases whose name contains "alias"
"""
import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date

os.environ.setdefault("OPIK_PROJECT_NAME", "book-keeper-evals")

from llm import get_curator_model  # noqa: E402
from memory.curator import Curator, Turn  # noqa: E402
from memory.models import FactView, PartyMemory  # noqa: E402
from memory.null import NullGraphMemory  # noqa: E402
from memory.recall import Recalled  # noqa: E402

TODAY = date(2026, 9, 30)

PEOPLE = {
    "Mama Chidi": PartyMemory(key="mama chidi", name="Mama Chidi", roles=("customer",),
                              aliases=("Chidi's mum", "OKAFOR CHINYERE MARY")),
    "Chidi": PartyMemory(key="chidi", name="Chidi"),
    "Emeka": PartyMemory(key="emeka", name="Emeka", roles=("customer",)),
    "Bola": PartyMemory(key="bola", name="Bola", roles=("customer",)),
    "Bolu": PartyMemory(key="bolu", name="Bolu"),
    "Pastor John": PartyMemory(key="pastor john", name="Pastor John", roles=("customer",)),
    "Alhaji Musa": PartyMemory(key="alhaji musa", name="Alhaji Musa", roles=("customer",)),
}
DNR = FactView(kind="do_not_remind", summary="do not remind", said="No remind Pastor John o", since="2026-09-08",
               confidence="stated")


@dataclass
class Case:
    name: str
    message: str
    people: list[str]
    expected: list[tuple]                 # (op, party[, alias]) the model should propose; [] = nothing
    history: list[tuple[str, str]] = field(default_factory=list)
    touched: list[dict] = field(default_factory=list)
    facts: dict[str, list[FactView]] = field(default_factory=dict)
    # proposals that are wrong but do nothing, because code ignores them (e.g. restating a
    # do-not-remind that's already remembered): reported, not failed
    harmless: list[tuple] = field(default_factory=list)


def sale(party, item="rice", entry_id=1):
    return {"id": entry_id, "type": "sale", "party": party, "item": item, "due_date": None, "outstanding": 0}


CASES = [
    Case("do-not-remind stated", "No remind Pastor John o, I go talk to am myself", ["Pastor John"],
         [("set_do_not_remind", "Pastor John")]),
    Case("trap: asking for a reminder isn't permission", "remind Pastor John", ["Pastor John"], [],
         history=[("user", "No remind Pastor John o"), ("assistant", "Okay, I won't remind Pastor John.")],
         facts={"Pastor John": [DNR]}, harmless=[("set_do_not_remind", "Pastor John")]),
    Case("do-not-remind lifted", "you fit remind Pastor John now, e don too long", ["Pastor John"],
         [("lift_do_not_remind", "Pastor John")], facts={"Pastor John": [DNR]}),
    Case("alias confirmed by the trader", "na Mama Chidi", ["Mama Chidi", "Chidi"],
         [("add_alias", "Mama Chidi", "chidi's mum")],
         history=[("user", "Chidi's mum take 2 bags garri, 30k"),
                  ("assistant", "Is Chidi's mum the same as Chidi or Mama Chidi, or someone new?")]),
    Case("trap: alias before the trader answers", "Chidi's mum take 2 bags garri, 30k, Friday",
         ["Mama Chidi", "Chidi"], []),
    Case("alias: misheard voice note", "Bolu na Bola, the voice thing spell am wrong", ["Bola", "Bolu"],
         [("add_alias", "Bola", "bolu")]),
    Case("role change: customer to supplier",
         "Alhaji Musa no dey buy from me again, na him dey supply my palm oil now. I buy 10 tins, 120k, pay am 70k",
         ["Alhaji Musa"], [("set_role", "Alhaji Musa", "customer"), ("set_role", "Alhaji Musa", "supplier")],
         touched=[{"id": 9, "type": "expense", "party": "Alhaji Musa", "item": "palm oil", "due_date": None,
                   "outstanding": 50000}]),
    Case("usual order stated", "Bola usual na 2 bags rice, she dey pay cash", ["Bola"],
         [("set_usual_order", "Bola")]),
    Case("trap: one sale isn't a usual order", "Bola buy 2 bags rice, 90k cash", ["Bola"], [],
         touched=[sale("Bola")]),
    Case("trap: a question", "Who owe me?", [], []),
    Case("trap: the porter is not Mama Chidi", "I pay Chidi 2k to carry the bags", ["Chidi", "Mama Chidi"], [],
         touched=[{"id": 3, "type": "expense", "party": "Chidi", "item": "carrying", "due_date": None,
                   "outstanding": 0}]),
    Case("alias removed", "Chidi's mum no be Mama Chidi o, na different person", ["Mama Chidi"],
         [("remove_alias", "Mama Chidi", "chidi's mum")]),
    Case("forget asked", "abeg forget everything you know about Emeka", ["Emeka"], [("forget_person", "Emeka")]),
    Case("trap: praise isn't forgetting", "Emeka don pay everything, e be good man", ["Emeka"], []),
    Case("usual order ended", "Bola no dey buy the usual again, forget that one", ["Bola"],
         [("remove_usual_order", "Bola")],
         facts={"Bola": [FactView(kind="usual_order", summary="2 bags rice", said=None, since="2026-09-19",
                                  confidence="inferred")]}),
    Case("trap: promises are the ledger's job", "Emeka beg make I give am till Friday", ["Emeka"], []),
]


def recalled_for(case: Case) -> Recalled:
    people = []
    for name in case.people:
        p = PEOPLE[name]
        people.append(PartyMemory(key=p.key, name=p.name, roles=p.roles, aliases=p.aliases,
                                  facts=tuple(case.facts.get(name, ()))))
    return Recalled(people=people, known_keys=frozenset(PEOPLE[n].key for n in PEOPLE))


def matches(op, want: tuple) -> bool:
    if op.op != want[0] or op.party.strip().lower() != want[1].lower():
        return False
    if len(want) == 3:
        if op.op == "set_role":
            return (op.role or "") == want[2]
        return (op.alias or "").strip().lower() == want[2]
    return True


def describe(op) -> str:
    extra = op.alias or op.role or ""
    if op.op == "set_role":
        extra += " on" if op.active is not False else " off"
    return f"{op.op}({op.party}{', ' + extra if extra else ''})"


async def run_case(curator: Curator, case: Case) -> tuple[bool, list, float, list]:
    turn = Turn(user_id=1, turn_id=None, message=case.message, reply="ok", channel="whatsapp",
                history=[{"role": r, "content": c} for r, c in case.history], touched=case.touched,
                recalled=recalled_for(case), today=TODAY)
    started = time.perf_counter()
    ops = await curator._propose(turn)
    elapsed = time.perf_counter() - started
    unmatched = list(ops)
    for want in case.expected:
        hit = next((op for op in unmatched if matches(op, want)), None)
        if hit is None:
            return False, ops, elapsed, []
        unmatched.remove(hit)
    harmless = [op for op in unmatched if any(matches(op, h) for h in case.harmless)]
    return len(unmatched) == len(harmless), ops, elapsed, harmless


async def main(filter_text: str = "") -> int:
    @asynccontextmanager
    async def no_db():
        yield None

    curator = Curator(NullGraphMemory(), no_db, model_factory=get_curator_model)
    model = os.getenv("CURATOR_MODEL") or os.getenv("LLM_MODEL") or "default"
    cases = [c for c in CASES if filter_text in c.name]
    print(f"Curator eval: {len(cases)} cases on {model}\n")
    passed = 0
    for case in cases:
        ok, ops, elapsed, harmless = await run_case(curator, case)
        passed += ok
        want = ", ".join(f"{w[0]}({', '.join(w[1:])})" for w in case.expected) or "nothing"
        got = ", ".join(describe(o) for o in ops) or "nothing"
        note = f"  [harmless extra: {', '.join(describe(o) for o in harmless)}; ignored by code]" if harmless else ""
        print(f"{'PASS' if ok else 'FAIL'}  {case.name}  ({elapsed:.1f}s){note}")
        if not ok:
            print(f"        wanted: {want}\n        got:    {got}")
    traps = [c for c in cases if not c.expected]
    print(f"\n{passed}/{len(cases)} passed  ·  traps: {len(traps)} cases where the right answer is nothing")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "")))

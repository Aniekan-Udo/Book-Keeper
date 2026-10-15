"""The agent team: routing picks the first agent, each agent sees only its own tools, and
an agent can hand a message on once."""
from langchain_core.messages import AIMessage

from agents.routing import AGENTS
from agents.team import AGENT_TOOLS, TurnContext, run_team
from tools import router
from tools.clean_version import build_tools


def ctx(text, **kw):
    return TurnContext(text=text, channel="web", history=[], tools=build_tools(None, 1, []), user_id=1, **kw)


class ScriptedAgents:
    """Stands in for router.handle_message. `handoffs[agent]` = who that agent passes to."""

    def __init__(self, monkeypatch, handoffs=None):
        self.runs: list[dict] = []
        self.handoffs = handoffs or {}
        monkeypatch.setattr(router, "handle_message", self)

    async def __call__(self, text, prompt, tools, history=None, callbacks=None):
        agent = next(a for a in AGENTS if f"You are the {a.split('_')[0].capitalize()}" in prompt)
        self.runs.append({"agent": agent, "tools": {t.name for t in tools}, "prompt": prompt})
        if agent in self.handoffs:
            transfer = next(t for t in tools if t.name == "transfer_to_agent")
            await transfer.ainvoke({"agent": self.handoffs[agent], "reason": "not mine"})
            return {"messages": [AIMessage(content="")]}
        return {"messages": [AIMessage(content=f"{agent} answered")]}


async def test_routing_picks_the_first_agent(monkeypatch):
    agents = ScriptedAgents(monkeypatch)
    result = await run_team(ctx("remind Emeka"))
    assert result.trail == ["collector"] and result.route_reason == "asks about reminders or chasing"
    assert result.result["messages"][-1].text == "collector answered"


async def test_each_agent_sees_only_its_tools(monkeypatch):
    agents = ScriptedAgents(monkeypatch)
    for text in ("I sold rice 5k", "remind Emeka", "Credit Alert Amt: NGN5,000"):
        await run_team(ctx(text))
    for run in agents.runs:
        assert run["tools"] == AGENT_TOOLS[run["agent"]] | {"transfer_to_agent"}
    recorder = next(r for r in agents.runs if r["agent"] == "recorder")
    assert "remind_debtor_tool" not in recorder["tools"] and "review_bank_alert_tool" not in recorder["tools"]


async def test_an_agent_can_hand_the_message_on(monkeypatch):
    agents = ScriptedAgents(monkeypatch, handoffs={"alert_matcher": "recorder"})
    # a pending alert routes "Bola buy rice" to the alert matcher, which passes it on
    result = await run_team(ctx("Bola buy 2 bags rice", pending_alerts=["#3: ₦45,000.00 in from BOLA"]))
    assert result.trail == ["alert_matcher", "recorder"]
    assert result.result["messages"][-1].text == "recorder answered"


async def test_only_one_handoff_per_message(monkeypatch):
    agents = ScriptedAgents(monkeypatch, handoffs={"recorder": "collector", "collector": "recorder"})
    result = await run_team(ctx("I sold rice"))
    assert result.trail == ["recorder", "collector"]  # the collector's handoff back is refused


async def test_memory_reaches_every_agent(monkeypatch):
    agents = ScriptedAgents(monkeypatch, handoffs={"recorder": "collector"})
    await run_team(ctx("I sold rice", memory="\n\n## What you remember about people in this message\n- Emeka"))
    assert all("## What you remember" in r["prompt"] for r in agents.runs)


async def test_a_claimed_but_unrecorded_sale_is_retried(monkeypatch):
    """Seen live: the Recorder replied "I don add sale: Pastor John…" without calling the
    tool. The team notices and runs it again, telling it nothing was saved."""
    calls = []

    async def lying_then_honest(text, prompt, tools, history=None, callbacks=None):
        calls.append(prompt)
        if len(calls) == 1:
            return {"messages": [AIMessage(content="I don add sale: Pastor John buy 1 bag beans, ₦30,000.")]}
        return {"messages": [AIMessage(content="", tool_calls=[{"name": "extract_transactions_tool", "args": {},
                                                                 "id": "1"}]),
                             AIMessage(content="I don add sale: Pastor John buy 1 bag beans, ₦30,000.")]}

    monkeypatch.setattr(router, "handle_message", lying_then_honest)
    result = await run_team(ctx("Pastor John take 1 bag beans, 30k, e never pay"))

    assert len(calls) == 2 and "no tool was called" in calls[1]
    assert result.result["messages"][-1].text.startswith("I don add sale")


async def test_honest_replies_are_not_retried(monkeypatch):
    calls = []

    async def asks(text, prompt, tools, history=None, callbacks=None):
        calls.append(prompt)
        return {"messages": [AIMessage(content="How much did Pastor John pay?")]}

    monkeypatch.setattr(router, "handle_message", asks)
    await run_team(ctx("Pastor John take 1 bag beans"))
    assert len(calls) == 1


def test_what_counts_as_claiming_a_recording():
    from agents.team import claims_unbacked_recording

    def says(text):
        return claims_unbacked_recording({"messages": [AIMessage(content=text)]})

    for claim in ("I don add sale: Pastor John", "Sale recorded: 2 bags", "I don correct am", "I have added it",
                  "Recorded.", "I don note say Mama Chidi pay"):
        assert says(claim), claim
    for honest in ("How much did he pay?", "You recorded 5 sales last week.", "Got am, I no go send reminder"):
        assert not says(honest), honest
    assert not claims_unbacked_recording({"messages": [AIMessage(content="Recorded.")]}, ledger_changed=True)

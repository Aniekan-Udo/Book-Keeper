"""The agent team for one trader message, as a LangGraph StateGraph.

    START ──route()──► recorder ─┐
                      collector ─┼─(handed off?)──► the other agent ──► END
                  alert_matcher ─┘            └─ no ──────────────────► END

Each agent is its own LangChain agent (tools/router.handle_message) with a short prompt
and only the tools for its job, plus `transfer_to_agent`. Routing is plain code
(agents/routing.py); the only routing a model decides is a handoff, and only one is
allowed per message, so a confused pair can't bounce a message back and forth.

The agents don't pass notes to each other: what one learns reaches the others through
the graph memory (memory/), which is the point of the design.
"""
import re
import time
from dataclasses import dataclass, field
from typing import Callable, TypedDict

from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from agents.routing import AGENTS, route
from tools import prompts, router
from utils import logger

MAX_HANDOFFS = 1

# Tools that change the books. A reply that says something was recorded must come from a
# run that called one of these.
LEDGER_TOOLS = frozenset({"extract_transactions_tool", "correct_transaction_tool", "undo_transaction_tool",
                          "review_bank_alert_tool", "resolve_bank_alert_tool"})
CHECKED_AGENTS = ("recorder", "alert_matcher")
# "I don record…", "I have added…", "Sale recorded", "Recorded.", "I don correct am", in
# English or Pidgin. Not "you recorded…" (a question about the past). A false alarm costs
# one extra model call; a miss leaves the trader believing a sale was saved.
_CLAIMS_RECORDING = re.compile(
    r"\bi\s*(don|have|'ve|has)\s+(record|add|log|note|save|correct|update|cancel|delete|remov)\w*"
    r"|^\W*recorded\b"
    r"|\b(sale|expense|payment|repayment|loan|entry|entries|it|am)\s+(has\s+been\s+|don\s+|is\s+)?recorded\b",
    re.IGNORECASE)
_UNBACKED_NUDGE = ("\n\n## Check\nYour previous attempt replied as if you had recorded or changed something, "
                   "but no tool was called, so nothing was saved. Call the tool now, or say plainly what's missing.")

# what each agent may do: its tools, by name (all built by tools/clean_version.build_tools)
AGENT_TOOLS: dict[str, frozenset[str]] = {
    "recorder": frozenset({
        "extract_transactions_tool", "query_transactions_tool", "generate_report_tool",
        "undo_transaction_tool", "correct_transaction_tool", "save_contact_tool",
        "customer_statement_tool", "stop_sharing_statement_tool",
    }),
    "collector": frozenset({
        "remind_debtor_tool", "confirm_reminder_tool", "save_contact_tool",
        "generate_report_tool", "query_transactions_tool",
    }),
    "alert_matcher": frozenset({
        "review_bank_alert_tool", "resolve_bank_alert_tool", "query_transactions_tool",
        "undo_transaction_tool", "correct_transaction_tool",
    }),
}


@dataclass
class TurnContext:
    """Everything the agents need for one message, prepared by tools/assistant.respond."""
    text: str
    channel: str
    history: list[dict]
    tools: list                       # all tools for this request; each agent gets its subset
    pending_amounts: list[str] = field(default_factory=list)
    reminder_drafts: list[str] = field(default_factory=list)
    pending_alerts: list[str] = field(default_factory=list)
    memory: str = ""                  # recalled memory, as a prompt section
    callbacks: list | None = None
    user_id: int | None = None        # for logs only
    touched: list | None = None       # entries recorded/corrected this turn (the same list build_tools fills)


class TeamState(TypedDict, total=False):
    agent: str            # who runs next
    handoff: str | None   # set by an agent that passed the message on
    hops: int
    trail: list[str]      # agents that ran, in order
    result: dict          # the last agent's run (LangChain messages)


@dataclass
class TeamResult:
    result: dict
    trail: list[str]
    route_reason: str


PROMPTS: dict[str, Callable[[TurnContext], str]] = {
    "recorder": lambda c: prompts.recorder_prompt(c.channel, c.text, c.pending_amounts, c.memory),
    "collector": lambda c: prompts.collector_prompt(c.channel, c.text, c.reminder_drafts, c.memory),
    "alert_matcher": lambda c: prompts.alert_matcher_prompt(c.channel, c.text, c.pending_alerts, c.memory),
}


def _handoff_tool(current: str, box: dict):
    allowed = [a for a in AGENTS if a != current]

    @tool
    async def transfer_to_agent(agent: str, reason: str = "") -> dict:
        """Pass this message to another agent when it isn't your job: recorder (recording, corrections, reports, statements), collector (payment reminders), alert_matcher (forwarded bank/POS alerts)."""
        if agent not in allowed:
            return {"error": f"agent must be one of {', '.join(allowed)}"}
        box["to"] = agent
        box["reason"] = reason
        return {"handed_off_to": agent, "note": "Done. Don't reply to the user."}

    return transfer_to_agent


def _tools_called(result: dict) -> set[str]:
    return {call["name"] for m in result.get("messages", []) for call in (getattr(m, "tool_calls", None) or [])}


def claims_unbacked_recording(result: dict, ledger_changed: bool = False) -> bool:
    """The reply says something was recorded, but no ledger tool ran in this attempt (judged
    by the tool calls in the run, and by whether any entry was actually written)."""
    messages = result.get("messages") or []
    reply = messages[-1].text if messages else ""
    if ledger_changed or _tools_called(result) & LEDGER_TOOLS:
        return False
    return bool(_CLAIMS_RECORDING.search(reply or ""))


def build_team(ctx: TurnContext):
    def node(name: str):
        async def run(state: TeamState) -> TeamState:
            box: dict = {}
            tools = [t for t in ctx.tools if t.name in AGENT_TOOLS[name]] + [_handoff_tool(name, box)]
            started = time.perf_counter()
            prompt = PROMPTS[name](ctx)
            written = len(ctx.touched) if ctx.touched is not None else 0
            result = await router.handle_message(ctx.text, prompt, tools, ctx.history, callbacks=ctx.callbacks)
            changed = ctx.touched is not None and len(ctx.touched) > written
            if name in CHECKED_AGENTS and not box.get("to") and claims_unbacked_recording(result, changed):
                # the model said it saved something it never saved: one more try, told so
                logger.warning("team.unbacked_claim", user_id=ctx.user_id, agent=name)
                result = await router.handle_message(ctx.text, prompt + _UNBACKED_NUDGE, tools, ctx.history,
                                                     callbacks=ctx.callbacks)
                changed = ctx.touched is not None and len(ctx.touched) > written
                if claims_unbacked_recording(result, changed):
                    logger.error("team.unbacked_claim_again", user_id=ctx.user_id, agent=name)
            hops = state.get("hops", 0)
            target = box.get("to")
            if target and hops >= MAX_HANDOFFS:
                logger.warning("team.handoff_refused", user_id=ctx.user_id, from_agent=name, to_agent=target)
                target = None
            logger.info("team.agent_ran", user_id=ctx.user_id, agent=name, handed_off_to=target,
                        ms=round((time.perf_counter() - started) * 1000, 1))
            return {"result": result, "handoff": target, "hops": hops + (1 if target else 0),
                    "trail": [*state.get("trail", []), name], "agent": target or name}
        return run

    graph = StateGraph(TeamState)
    for name in AGENTS:
        graph.add_node(name, node(name))
    graph.add_conditional_edges(START, lambda s: s["agent"], {a: a for a in AGENTS})
    for name in AGENTS:
        graph.add_conditional_edges(name, lambda s: s.get("handoff") or END, {**{a: a for a in AGENTS}, END: END})
    return graph.compile()


async def run_team(ctx: TurnContext) -> TeamResult:
    """Route the message and run the agents. LLM failures (router.LLMUnavailable) propagate
    to the caller, as before the split."""
    agent, reason = route(ctx.text, ctx.pending_alerts, ctx.reminder_drafts)
    logger.info("team.routed", user_id=ctx.user_id, agent=agent, reason=reason)
    state = await build_team(ctx).ainvoke({"agent": agent, "hops": 0, "trail": []})
    return TeamResult(result=state["result"], trail=state["trail"], route_reason=reason)

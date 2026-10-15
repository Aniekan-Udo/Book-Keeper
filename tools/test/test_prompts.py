"""The instructions: each agent gets the shared rules plus only its own section, and the
router sends each message to the right agent."""
import pytest

from agents.routing import route
from tools.prompts import alert_matcher_prompt, collector_prompt, recorder_prompt

SHARED_RULES = ("never receive, send, collect", "Never ask for bank logins", "Reply in the user's style",
                "transfer_to_agent")


@pytest.mark.parametrize("build", [recorder_prompt, collector_prompt, alert_matcher_prompt])
def test_every_agent_gets_the_money_and_safety_rules(build):
    prompt = build("whatsapp", message="hello")
    for rule in SHARED_RULES:
        assert rule in prompt, rule


def test_recorder_has_the_recording_rules_only():
    prompt = recorder_prompt("whatsapp", message="I sold 20 bags of rice for 400k")
    for rule in ("Amounts: never guess", "debt_repayment", "due_date"):
        assert rule in prompt, rule
    assert "## Bank and POS alerts" not in prompt and "## Payment reminders" not in prompt


def test_collector_and_alert_matcher_have_their_own_sections():
    collector = collector_prompt("whatsapp", message="remind Emeka")
    assert "## Payment reminders" in collector and "wait for their yes" in collector
    assert "## Examples" not in collector  # recording examples are the Recorder's
    alerts = alert_matcher_prompt("whatsapp", message="Credit Alert")
    assert "## Bank and POS alerts" in alerts and "## Examples" not in alerts


def test_voice_notes_get_the_voice_section():
    assert "## Voice notes" in recorder_prompt(message="(voice note) I sell rice")
    assert "## Voice notes" not in recorder_prompt(message="I sell rice")


def test_waiting_items_and_memory_reach_the_right_prompt():
    assert "#9: sale — rice" in recorder_prompt(pending_amounts=["#9: sale — rice (Ada), 2026-09-28"], message="15k")
    assert "#4: to Emeka" in collector_prompt(reminder_drafts=["#4: to Emeka for ₦16,000.00"], message="yes")
    assert "#3: ₦45,000.00 in from BOLA" in alert_matcher_prompt(pending_alerts=["#3: ₦45,000.00 in from BOLA"])
    memory = "\n\n## What you remember about people in this message\n- Emeka: customer"
    assert recorder_prompt(message="Emeka paid", memory=memory).endswith(memory)


@pytest.mark.parametrize("message, agent", [
    ("I sold 20 bags of rice for 400k", "recorder"),
    ("who owe me?", "recorder"),
    ("(forwarded alert screenshot) Credit Alert Amt: NGN10,000.00", "alert_matcher"),
    ("Acct: 012****345 Amt: NGN10,000.00 CR Desc: TRF FROM MAMA CHIDI", "alert_matcher"),
    ("Credit alert! 5,000 received", "alert_matcher"),
    ("remind Mama Chidi", "collector"),
    ("abeg tell Emeka make he pay", "collector"),
    ("who I suppose chase?", "collector"),
])
def test_routing_by_message(message, agent):
    assert route(message)[0] == agent


def test_routing_by_what_is_waiting():
    drafts = ["#4: to Emeka for ₦16,000.00"]
    alerts = ["#3: ₦45,000.00 in from BOLA"]
    assert route("yes send am", reminder_drafts=drafts)[0] == "collector"
    assert route("Bola buy 2 bags rice", reminder_drafts=drafts)[0] == "recorder"  # not an answer to the draft
    assert route("na Mama Chidi", pending_alerts=alerts)[0] == "alert_matcher"
    assert route("na sale", pending_alerts=alerts, reminder_drafts=drafts)[0] == "alert_matcher"

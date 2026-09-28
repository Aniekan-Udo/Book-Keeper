"""The instructions: compact core always sent, specialist sections only when needed."""
import pytest

from tools.prompts import router_sys_msg


def test_core_rules_are_always_there():
    prompt = router_sys_msg("whatsapp", message="I sold rice 5k")
    for rule in ("Amounts: never guess", "debt_repayment", "wait for their yes", "never receive, send, collect",
                 "Never ask for bank logins", "Reply in the user's style"):
        assert rule in prompt, rule


def test_plain_message_gets_no_specialist_sections():
    prompt = router_sys_msg("whatsapp", message="I sold 20 bags of rice for 400k")
    assert "## Voice notes" not in prompt and "## Bank and POS alerts" not in prompt
    assert "## Payment reminders" not in prompt


@pytest.mark.parametrize("message, section", [
    ("(voice note) I sell rice", "## Voice notes"),
    ("(forwarded alert screenshot) Credit Alert Amt: NGN10,000.00", "## Bank and POS alerts"),
    ("Acct: 012****345 Amt: NGN10,000.00 CR Desc: TRF FROM MAMA CHIDI", "## Bank and POS alerts"),
    ("Credit alert! 5,000 received", "## Bank and POS alerts"),
    ("remind Mama Chidi", "## Payment reminders"),
    ("abeg tell Emeka make he pay", "## Payment reminders"),
])
def test_specialist_sections_when_the_message_needs_them(message, section):
    assert section in router_sys_msg("whatsapp", message=message)


def test_waiting_items_bring_their_sections():
    assert "## Bank and POS alerts" in router_sys_msg(pending_alerts=["#3: ₦45,000.00 in from BOLA"], message="na sale")
    prompt = router_sys_msg(reminder_drafts=["#4: to Emeka for ₦16,000.00"], message="yes")
    assert "## Payment reminders" in prompt and "#4: to Emeka" in prompt
    assert "#9: sale — rice" in router_sys_msg(pending_amounts=["#9: sale — rice (Ada), 2026-09-28"], message="15k")

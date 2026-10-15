"""Which agent takes a message. Plain code, no model call: the signals are easy to read
(a forwarded alert looks like one; "remind" means reminders), and skipping a "supervisor"
model call keeps the token budget. When routing is wrong, the agent that got the message
hands it on (agents/team.py), so a miss costs one extra call, not a wrong answer.
"""
import re

from tools.prompts import ALERT_HINT, REMINDER_HINT

AGENTS = ("recorder", "collector", "alert_matcher")

# short answers to a pending reminder draft
_DRAFT_ANSWER = re.compile(
    r"^\s*(yes|yeah|yea|yep|ok|okay|sure|send( am| it)?|go ahead|abeg send|no|nope|cancel|don'?t( send)?|leave am)\b",
    re.IGNORECASE)


def route(message: str, pending_alerts: list[str] | None = None,
          reminder_drafts: list[str] | None = None) -> tuple[str, str]:
    """(agent, why). The why goes to the logs and the trace."""
    if ALERT_HINT.search(message):
        return "alert_matcher", "looks like a forwarded bank alert"
    if REMINDER_HINT.search(message):
        return "collector", "asks about reminders or chasing"
    if reminder_drafts and _DRAFT_ANSWER.search(message):
        return "collector", "answers a reminder draft"
    if pending_alerts:
        return "alert_matcher", "a bank alert is waiting for an answer"
    return "recorder", "default"

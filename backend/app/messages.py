"""Every user-facing string the backend sends (Telegram, contact page, replies).

Wording rules (BUILD_PLAN §7.3) are enforced by tests/test_wording.py, which
renders every template and greps the source tree for banned stems. Alert text
stays minimal and carries no health data: Telegram bot chats are not end-to-end
encrypted.
"""
from __future__ import annotations

from typing import Optional

from .states import FINAL_BANNER

# Wearer-facing (the "Reminders" chat the Apple Watch mirrors as a silent tap).
NOTED = "Noted ✓"
CANCELLED = "Cancelled ✓"
ENDED = "Ended ✓"

BTN_ON_IT = "I'm on it"
BTN_PASS = "Can't help – pass it on"
BTN_LIVE = "Open live page"
BTN_SAFE = "I'm with her – she's safe"
BTN_CANT_REACH = "Can't reach her"

SIMULATED_SERVICES_NOTE = FINAL_BANNER + ". No call is placed."


def _location_line(loc: Optional[dict]) -> str:
    if not loc:
        return "Last known location: not available."
    sim = " (simulated)" if loc.get("simulated") else ""
    return (f"Last known location{sim} (as of {loc['as_of']}, {loc['age_min']} min old): "
            f"{loc['link']}")


def render(template: str, p: dict) -> str:
    name = p.get("name") or "She"
    t = p.get("time", "")
    if template == "reminder":
        return "Reminder"
    if template == "reminder_n":
        return f"Reminder {p['n']}"
    if template == "home_safe":
        return "Home safe?"
    if template == "noted":
        return NOTED
    if template == "noted_alerting":
        return f"{NOTED} Your contacts are still checking on you."
    if template == "cancelled":
        return CANCELLED
    if template == "ended":
        head = p.get("head") or ""
        return f"{ENDED} Timeline head: {head[:16]}" if head else ENDED
    if template == "wearer_lost_contact":
        return f"StillHere lost contact with your device at {t}. Your check-in schedule is still running."
    if template == "wearer_restored":
        return f"StillHere: contact with your device restored at {t}."
    if template == "alert_no_response":
        times = ", ".join(x for x in (p.get("t1"), p.get("t2")) if x) or t
        return (f"StillHere: {name} hasn't responded to 2 check-ins ({times}). "
                f"This is not a confirmed emergency. {_location_line(p.get('location'))}")
    if template == "alert_duress":
        return (f"StillHere: SILENT HELP REQUEST from {name}. She may not be able to talk. "
                f"Don't call her phone. {_location_line(p.get('location'))}")
    if template == "alert_final":
        return (f"StillHere: no one has confirmed {name} is safe yet. {SIMULATED_SERVICES_NOTE} "
                f"Please check on her. {_location_line(p.get('location'))}")
    if template == "lost_contact":
        return (f"StillHere: We lost contact with {name}'s device at {t}. Cause unknown. "
                f"Her check-in schedule is still running.")
    if template == "restored":
        return f"StillHere: Contact with {name}'s device restored at {t}."
    if template == "late_device":
        late = " (delivered late)" if p.get("late") else ""
        return f"StillHere: {name} responded on her device at {t}{late} – please still check on her."
    if template == "late_phone":
        return f"StillHere: {name} checked in on her phone at {t} – please still check on her."
    if template == "handling":
        return f"StillHere: {p.get('contact') or 'Another contact'} is handling it."
    if template == "ack_thanks":
        return "You're handling it. Tap below once you're with her, or if you can't reach her."
    if template == "resolved":
        return f"StillHere: Resolved by {p.get('contact') or 'a contact'} at {t}."
    if template == "false_alarm":
        return f"StillHere: False alarm: {name} cancelled with her PIN at {t}."
    if template == "duress_pin_note":
        return f"StillHere: {name} entered her cancel PIN. Please confirm in person."
    if template == "contact_approved":
        return (f"You're {name}'s trusted contact #{p.get('n', 1)}. You'll see nothing from StillHere "
                f"unless she misses check-ins or asks for help.")
    if template == "contact_pending":
        return f"Thanks. {name} needs to approve you before you become a trusted contact."
    if template == "wearer_linked":
        return "This chat will get your StillHere reminders."
    if template == "invite_invalid":
        return "This invite link has expired or was already used."
    if template == "private_chat_only":
        return "Open this link in a private chat with the bot, not in a group."
    raise KeyError(template)


TEMPLATES = (
    "reminder", "reminder_n", "home_safe", "noted", "noted_alerting", "cancelled", "ended",
    "wearer_lost_contact", "wearer_restored", "alert_no_response", "alert_duress", "alert_final",
    "lost_contact", "restored", "late_device", "late_phone", "handling", "ack_thanks", "resolved",
    "false_alarm", "duress_pin_note", "contact_approved", "contact_pending", "wearer_linked",
    "invite_invalid", "private_chat_only",
)

# Contact-page wording per alert kind (no health data, no session internals).
CONTACT_PAGE_TITLES = {
    "NO_RESPONSE": "{name} hasn't responded to 2 check-ins. This is not a confirmed emergency.",
    "DURESS": "SILENT HELP REQUEST. She may not be able to talk. Don't call her phone.",
    "LOST_CONTACT": "We lost contact with {name}'s device. Cause unknown. Her check-in schedule is still running.",
}

REPLY_MESSAGES = {
    "cancelled": CANCELLED,
    "ended": ENDED,
    "noted": NOTED,
}

CALLBACK_ANSWERS = {
    "acked": "Thanks, you're on it.",
    "already_handled": "Someone is already handling it.",
    "passed": "Passed on to the next contact.",
    "resolved": "Thank you. Marked safe.",
    "escalated": "Alerting the next contact.",
    "ignored": "This alert is no longer active.",
}

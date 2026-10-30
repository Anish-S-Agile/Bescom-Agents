"""
Common organisation emails as templates. Placeholders use {name}; each template declares
which variables are required so the orchestrator can ask for anything missing.
"""
import re
from typing import Any, Dict, List, Optional

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "meeting_invite": {
        "description": "Invite people to a meeting",
        "required": ["topic", "date_time"],
        "subject": "Meeting invitation: {topic}",
        "body": (
            "Hello {recipient_name},\n\n"
            "You are invited to a meeting on {topic}.\n\n"
            "When: {date_time}\n"
            "Where: {location}\n"
            "Agenda: {agenda}\n\n"
            "Please confirm your availability.\n\n"
            "Regards,\n{sender_name}"),
        "defaults": {"location": "To be confirmed", "agenda": "To be shared"},
    },
    "announcement": {
        "description": "General organisation-wide announcement",
        "required": ["topic", "details"],
        "subject": "Announcement: {topic}",
        "body": "Hello {recipient_name},\n\nWe would like to announce: {topic}.\n\n{details}\n\nRegards,\n{sender_name}",
        "defaults": {},
    },
    "reminder": {
        "description": "Reminder about a task, payment or deadline",
        "required": ["topic", "due"],
        "subject": "Reminder: {topic}",
        "body": (
            "Hello {recipient_name},\n\nThis is a friendly reminder regarding {topic}, due {due}.\n\n"
            "{details}\n\nRegards,\n{sender_name}"),
        "defaults": {"details": "Please take the necessary action at the earliest."},
    },
    "follow_up": {
        "description": "Follow up on a previous conversation",
        "required": ["topic"],
        "subject": "Following up: {topic}",
        "body": (
            "Hello {recipient_name},\n\nI am following up on {topic}.\n\n{details}\n\n"
            "Looking forward to your response.\n\nRegards,\n{sender_name}"),
        "defaults": {"details": "Please let me know if you need any further information."},
    },
    "thank_you": {
        "description": "Thank someone",
        "required": ["topic"],
        "subject": "Thank you",
        "body": "Hello {recipient_name},\n\nThank you for {topic}. We truly appreciate it.\n\nRegards,\n{sender_name}",
        "defaults": {},
    },
    "outage_notice": {
        "description": "Planned or unplanned service outage notice",
        "required": ["system", "start_time"],
        "subject": "Service notice: {system}",
        "body": (
            "Hello {recipient_name},\n\n{system} will be affected from {start_time}.\n\n"
            "Impact: {impact}\nExpected resolution: {resolution}\n\n"
            "We apologise for the inconvenience.\n\nRegards,\n{sender_name}"),
        "defaults": {"impact": "Intermittent unavailability", "resolution": "We will share an update shortly"},
    },
    "welcome": {
        "description": "Welcome a new customer, employee or partner",
        "required": ["organization"],
        "subject": "Welcome to {organization}",
        "body": (
            "Hello {recipient_name},\n\nWelcome to {organization}! We are glad to have you with us.\n\n"
            "{details}\n\nRegards,\n{sender_name}"),
        "defaults": {"details": "Please reach out if you need any help getting started."},
    },
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def list_templates() -> Dict[str, str]:
    return {name: t["description"] for name, t in TEMPLATES.items()}


def missing_variables(template: str, variables: Dict[str, Any]) -> List[str]:
    t = TEMPLATES.get(template)
    if not t:
        return []
    return [k for k in t["required"] if not str((variables or {}).get(k, "")).strip()]


def render_template(template: str, variables: Dict[str, Any], default_sender: str = "") -> Optional[Dict[str, str]]:
    """Render subject/body; returns None for an unknown template. Raises ValueError on missing required vars."""
    t = TEMPLATES.get(template)
    if not t:
        return None
    missing = missing_variables(template, variables)
    if missing:
        raise ValueError(f"missing template variables: {', '.join(missing)}")
    values = {"recipient_name": "there", "sender_name": default_sender or "The Team", "details": ""}
    values.update(t["defaults"])
    values.update({k: str(v) for k, v in (variables or {}).items() if str(v).strip()})

    def fill(text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), text)

    body = re.sub(r"\n{3,}", "\n\n", fill(t["body"])).strip()
    return {"subject": fill(t["subject"]).strip(), "body": body}

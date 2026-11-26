"""
Slack messages for a mirrored email thread.

Text taken from an email (subject, names, addresses, body, file names) is
untrusted: it goes into ``plain_text`` objects or rich-text ``text`` elements,
neither of which Slack parses, or into ``mrkdwn`` only after escaping and with
``verbatim`` set, so it can never become a mention, a link or formatting.
"""

from datetime import datetime

import frappe
from frappe.utils import format_datetime, get_datetime, get_url_to_form, now_datetime

from frappe_slack_connector.helpers.str_utils import escape_slack_text

# Slack caps a section's text at 3000 characters and a header's at 150
BODY_LIMIT = 2900
HEADER_LIMIT = 150
MAX_PARTICIPANTS = 10
MAX_ATTACHMENT_NAMES = 10
# An email older than this when it is posted gets its date shown: Slack's own
# timestamp is the time of posting, which says nothing about history sent in one go
OLD_EMAIL_SECONDS = 3600

SEPARATOR = "  ·  "
DATE_FORMAT = "d MMM yyyy, HH:mm"
BODY_CUT_NOTE = "\n… (continued in the ERP)"
STOPPED_TEXT = "Mirroring of this email thread was stopped."
RESUMED_TEXT = "Mirroring of this email thread was resumed."


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": False}


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def _subject(doc) -> str:
    return (doc.subject_of_first_mail or "").strip() or "(no subject)"


def _sender_name(email) -> str:
    return (email.sender_full_name or email.sender or "").strip() or "Unknown sender"


def _rich_text(text: str, bold: bool = False) -> dict:
    """A rich-text element: Slack shows its text as it is, whatever characters it holds."""
    element = {"type": "text", "text": text}
    if bold:
        element["style"] = {"bold": True}
    return element


def _body(email) -> str:
    body = (email.plain_content or "").strip() or "(no text)"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT].rstrip() + BODY_CUT_NOTE
    return body


def _listed(items: list[str], limit: int) -> str:
    shown = items[:limit]
    extra = len(items) - len(shown)
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")


def _participants(doc) -> str:
    seen: list[str] = []
    for email in doc.emails or []:
        for address in [email.sender, *(email.recipients or "").split(",")]:
            address = (address or "").strip()
            if address and address not in seen:
                seen.append(address)
    return _listed(seen, MAX_PARTICIPANTS) or "unknown"


def _attachment_names(email) -> list[str]:
    data = email.attachments_data
    if isinstance(data, str):
        try:
            data = frappe.parse_json(data)
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    return [str(row["file_name"]) for row in data if isinstance(row, dict) and row.get("file_name")]


def _is_old(email, now: datetime) -> bool:
    if not email.date_and_time:
        return False
    return (now - get_datetime(email.date_and_time)).total_seconds() > OLD_EMAIL_SECONDS


def sorted_emails(doc) -> list:
    """The thread's emails oldest first; rows without a date keep their place at the end."""
    return sorted(
        doc.emails or [],
        key=lambda email: (
            not email.date_and_time,
            get_datetime(email.date_and_time) if email.date_and_time else datetime.min,
            email.idx or 0,
        ),
    )


def parent_text(doc) -> str:
    """Notification fallback for the parent message."""
    return escape_slack_text(f"Email thread: {_subject(doc)}")


def parent_blocks(doc) -> list[dict]:
    links = [f"<{get_url_to_form('Gmail Thread', doc.name)}|Open thread in ERP>"]
    if doc.reference_doctype and doc.reference_name:
        label = escape_slack_text(f"{doc.reference_doctype}: {doc.reference_name}")
        links.append(f"<{get_url_to_form(doc.reference_doctype, doc.reference_name)}|{label}>")
    return [
        {"type": "header", "text": _plain(_subject(doc)[:HEADER_LIMIT])},
        {"type": "section", "text": _plain(f"Participants: {_participants(doc)}")},
        {"type": "context", "elements": [_mrkdwn(SEPARATOR.join([":email: Email thread", *links]))]},
    ]


def reply_text(email) -> str:
    """Notification fallback for one email."""
    return escape_slack_text(f"{_sender_name(email)}: {_body(email)[:150]}")


def reply_blocks(email, now: datetime | None = None) -> list[dict]:
    header = [_rich_text(_sender_name(email), bold=True)]
    if _is_old(email, now or now_datetime()):
        header.append(_rich_text(SEPARATOR + format_datetime(email.date_and_time, DATE_FORMAT)))
    blocks = [
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": header}]},
        {"type": "section", "text": _plain(_body(email))},
    ]
    names = _attachment_names(email)
    if names:
        blocks.append({"type": "context", "elements": [_plain(f"Attachments: {_listed(names, MAX_ATTACHMENT_NAMES)}")]})
    return blocks


def stopped_blocks(user_full_name: str) -> list[dict]:
    text = f"Mirroring of this email thread was stopped by {user_full_name}. New emails will not appear here."
    return [{"type": "section", "text": _plain(text)}]


def resumed_blocks(user_full_name: str) -> list[dict]:
    return [{"type": "section", "text": _plain(f"Mirroring of this email thread was resumed by {user_full_name}.")}]

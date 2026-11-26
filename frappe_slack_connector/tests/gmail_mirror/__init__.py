"""Fixtures for the Gmail thread mirror tests."""

import frappe
from frappe.utils import add_to_date
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.gmail_mirror import MIRROR, SETTING, is_gmail_thread_installed
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID

FIRST_EMAIL_AT = "2026-01-05 10:00:00"


def requires_gmail_thread(test_class):
    """Skip a test class on a site without the Gmail Thread app; checked when a test runs, not at import."""
    original_set_up = test_class.setUp

    def set_up(self):
        if not is_gmail_thread_installed():
            self.skipTest("frappe_gmail_thread is not installed")
        original_set_up(self)

    test_class.setUp = set_up
    return test_class


def set_mirroring(enabled: bool) -> None:
    frappe.db.set_single_value("Slack Settings", SETTING, int(enabled))


def _email_row(thread_id: str, n: int, **fields) -> dict:
    return {
        "gmail_message_id": f"{thread_id}-m{n}",
        "subject": "Quarterly plan",
        "sender": f"sender{n}@example.com",
        "sender_full_name": f"Sender {n}",
        "recipients": "team@example.com",
        "plain_content": f"Body {n}",
        "sent_or_received": "Received",
        "date_and_time": add_to_date(FIRST_EMAIL_AT, minutes=n - 1),
        **fields,
    }


def make_test_thread(thread_id: str, emails: int = 2, **fields):
    """Insert a Gmail Thread with the given number of emails, one minute apart."""
    return frappe.get_doc(
        {
            "doctype": "Gmail Thread",
            "gmail_thread_id": thread_id,
            "status": "Open",
            "subject_of_first_mail": "Quarterly plan",
            "emails": [_email_row(thread_id, n) for n in range(1, emails + 1)],
            **fields,
        }
    ).insert(ignore_permissions=True)


def add_test_email(thread, n: int, **fields):
    """Append email number ``n`` to the thread and save it, the way the Gmail sync does."""
    doc = frappe.get_doc("Gmail Thread", thread.name)
    doc.append("emails", _email_row(thread.name, n, **fields))
    doc.save(ignore_permissions=True)
    return doc


def make_test_mirror(thread_id: str, **fields):
    return frappe.get_doc(
        {
            "doctype": MIRROR,
            "gmail_thread": thread_id,
            "channel_id": TEST_SLACK_CHANNEL_ID,
            "channel_name": "test-fsc",
            "status": "Active",
            **fields,
        }
    ).insert(ignore_permissions=True)


def api_error(code: str) -> SlackApiError:
    return SlackApiError(message=code, response={"ok": False, "error": code})


def channel_info(**flags) -> dict:
    """A conversations.info response for a healthy channel, with the given flags changed."""
    channel = {
        "id": TEST_SLACK_CHANNEL_ID,
        "is_member": True,
        "is_archived": False,
        "is_ext_shared": False,
        "is_pending_ext_shared": False,
    }
    return {"ok": True, "channel": channel | flags}

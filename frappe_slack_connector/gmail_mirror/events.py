"""Document event handlers for the Gmail thread mirror."""

import frappe

from frappe_slack_connector.gmail_mirror import MIRROR, is_switched_on
from frappe_slack_connector.gmail_mirror.job import enqueue_post
from frappe_slack_connector.helpers.error import generate_error_log


def on_thread_update(doc, method=None) -> None:
    """
    Gmail Thread on_update: queue the post job for the thread's active mirror.

    This runs inside the Gmail sync's save of the thread, so it only reads the
    mirror table and queues a job, changes nothing on the document, and never
    raises: an error here would make the sync skip the email it is saving.
    """
    try:
        if not is_switched_on():
            return
        for mirror in frappe.get_all(MIRROR, filters={"gmail_thread": doc.name, "status": "Active"}, pluck="name"):
            enqueue_post(mirror, after_commit=True)
    except Exception as exc:
        generate_error_log("Gmail thread mirror: could not queue new emails", exception=exc)

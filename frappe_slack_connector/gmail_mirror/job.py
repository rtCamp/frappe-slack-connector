"""
The background job that keeps a mirrored email thread current in Slack.

One function serves the first fill, later emails and retries: it posts
whatever the Slack thread is missing. It never writes to the Gmail Thread, and
it writes to its own mirror row field by field, so a Stop pressed on the form
while it runs is not overwritten.
"""

import time

import frappe
from frappe.utils import cint
from frappe.utils.file_lock import LockTimeoutError

from frappe_slack_connector.gmail_mirror import MIRROR, MIRROR_MESSAGE, is_enabled
from frappe_slack_connector.gmail_mirror.messages import (
    parent_blocks,
    parent_text,
    reply_blocks,
    reply_text,
    sorted_emails,
)
from frappe_slack_connector.gmail_mirror.slack import (
    FATAL_ERRORS,
    PROBLEM_MESSAGES,
    channel_problem,
    mirror_client,
    slack_error_code,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.locks import job_lock

POST_JOB = "frappe_slack_connector.gmail_mirror.job.post_missing"
# Seconds a run waits for an earlier run of the same mirror
LOCK_TIMEOUT = 600
# Slack allows about one message a second per channel
POST_INTERVAL_SECONDS = 1
# Failed runs in a row after which a mirror stops retrying by itself
MAX_RETRIES = 5
ERROR_TEXT_LIMIT = 500


class MirrorCannotContinue(Exception):
    """A condition that trying again will not change; the message is shown to the user."""


def enqueue_post(mirror: str, after_commit: bool = False, scheduled: bool = False) -> None:
    # No job id: the lock serialises runs and a surplus run finds nothing to do, while
    # dropping a duplicate would lose an email that arrives during a run
    frappe.enqueue(POST_JOB, queue="long", mirror=mirror, scheduled=scheduled, enqueue_after_commit=after_commit)


def retry_pending() -> None:
    """Scheduler entry: run again every active mirror whose last run failed."""
    if not is_enabled():
        return
    for mirror in frappe.get_all(MIRROR, filters={"status": "Active", "retry_count": [">", 0]}, pluck="name"):
        enqueue_post(mirror, scheduled=True)


def post_missing(mirror: str, scheduled: bool = False) -> None:
    """
    Post to Slack whatever the mirror's thread is missing. Never raises: a
    failure is recorded on the mirror. ``scheduled`` marks a run started by the
    retry schedule; only those count towards MAX_RETRIES.
    """
    if not is_enabled():
        return
    try:
        with job_lock(f"gmail_mirror_{mirror}", LOCK_TIMEOUT):
            _post_missing(mirror)
    except Exception as exc:
        frappe.db.rollback()
        try:
            _record_failure(mirror, exc, scheduled)
        except Exception as record_exc:
            generate_error_log("Gmail thread mirror: could not record a failure", exception=record_exc)


def _post_missing(mirror: str) -> None:
    state = frappe.db.get_value(
        MIRROR,
        mirror,
        ["gmail_thread", "channel_id", "channel_name", "thread_ts", "status", "retry_count", "last_error"],
        as_dict=True,
    )
    if not state or state.status != "Active":
        return
    if not frappe.db.exists("Gmail Thread", state.gmail_thread):
        raise MirrorCannotContinue("The email thread no longer exists.")

    client = mirror_client()
    # Checked on every run: the channel may have been archived, left or shared with another organisation since
    problem = channel_problem(client, state.channel_id)
    if problem:
        raise MirrorCannotContinue(PROBLEM_MESSAGES[problem].format(state.channel_name or state.channel_id))

    doc = frappe.get_doc("Gmail Thread", state.gmail_thread)
    thread_ts = state.thread_ts
    if not thread_ts:
        response = client.chat_postMessage(
            channel=state.channel_id,
            text=parent_text(doc),
            blocks=parent_blocks(doc),
            unfurl_links=False,
            unfurl_media=False,
        )
        thread_ts = response["ts"]
        frappe.db.set_value(MIRROR, mirror, "thread_ts", thread_ts, update_modified=False)
        frappe.db.commit()  # nosemgrep

    posted_in_this_run = False
    while True:
        posted = frappe.get_all(MIRROR_MESSAGE, filters={"parent": mirror, "parenttype": MIRROR}, pluck="email_key")
        missing = [email for email in sorted_emails(doc) if email.name not in posted]
        if not missing:
            break
        for email in missing:
            if posted_in_this_run:
                time.sleep(POST_INTERVAL_SECONDS)
            # Stop may have been pressed since the last post, the pause included
            if not _is_active(mirror):
                return
            response = client.chat_postMessage(
                channel=state.channel_id,
                thread_ts=thread_ts,
                text=reply_text(email),
                blocks=reply_blocks(email),
                unfurl_links=False,
                unfurl_media=False,
            )
            posted_in_this_run = True
            posted.append(email.name)
            frappe.get_doc(
                {
                    "doctype": MIRROR_MESSAGE,
                    "parent": mirror,
                    "parenttype": MIRROR,
                    "parentfield": "messages",
                    "idx": len(posted),
                    "email_key": email.name,
                    "slack_ts": response["ts"],
                }
            ).insert(ignore_permissions=True)
            # Committed per email: a later failure must not make this one post again
            frappe.db.commit()  # nosemgrep
        # Emails that synced while this run was posting
        doc = frappe.get_doc("Gmail Thread", state.gmail_thread)

    if cint(state.retry_count) or state.last_error:
        frappe.db.set_value(MIRROR, mirror, {"retry_count": 0, "last_error": ""}, update_modified=False)
        frappe.db.commit()  # nosemgrep


def _is_active(mirror: str) -> bool:
    # A new transaction first: the one in progress cannot see a Stop committed after it began
    frappe.db.commit()  # nosemgrep
    return frappe.db.get_value(MIRROR, mirror, "status") == "Active"


def _record_failure(mirror: str, exc: Exception, scheduled: bool) -> None:
    """
    Note a failed run on the mirror. A failure that trying again cannot fix
    fails the mirror at once. Any other marks the mirror for the retry
    schedule, which runs it again until MAX_RETRIES of its runs in a row have
    failed. Runs started by an arriving email do not count: several emails
    during a short outage must not use up the retries within seconds. Nor does
    a run that waited too long for the lock, since the run holding it is alive.
    """
    generate_error_log("Gmail thread mirror: posting to Slack failed", message=f"Mirror {mirror}", exception=exc)
    state = frappe.db.get_value(MIRROR, mirror, ["status", "retry_count", "channel_name"], as_dict=True)
    # Stopped (or deleted) while the run was failing: leave it as the user set it
    if not state or state.status != "Active":
        return
    values = {"last_error": _error_text(exc, state.channel_name)}
    if isinstance(exc, MirrorCannotContinue) or slack_error_code(exc) in FATAL_ERRORS:
        values["status"] = "Failed"
    elif scheduled and not isinstance(exc, LockTimeoutError):
        values["retry_count"] = cint(state.retry_count) + 1
        if values["retry_count"] >= MAX_RETRIES:
            values["status"] = "Failed"
    else:
        values["retry_count"] = max(cint(state.retry_count), 1)
    frappe.db.set_value(MIRROR, mirror, values, update_modified=False)
    frappe.db.commit()  # nosemgrep


def _error_text(exc: Exception, channel_name: str | None) -> str:
    """The reason shown on the Gmail Thread form."""
    if isinstance(exc, LockTimeoutError):
        return "Timed out waiting for an earlier run of this mirror."
    code = slack_error_code(exc)
    if code in PROBLEM_MESSAGES:
        return PROBLEM_MESSAGES[code].format(channel_name or "")
    if code:
        return f"Slack error: {code}"
    return (str(exc) or type(exc).__name__)[:ERROR_TEXT_LIMIT]

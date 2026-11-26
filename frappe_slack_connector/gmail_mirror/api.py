"""What the Gmail Thread form calls: the state of a thread's mirror, and start, stop and retry."""

import frappe
from frappe import _
from frappe.utils import get_fullname
from frappe.utils.file_lock import LockTimeoutError

from frappe_slack_connector.gmail_mirror import MIRROR, is_enabled
from frappe_slack_connector.gmail_mirror.job import enqueue_post
from frappe_slack_connector.gmail_mirror.messages import RESUMED_TEXT, STOPPED_TEXT, resumed_blocks, stopped_blocks
from frappe_slack_connector.gmail_mirror.slack import PROBLEM_MESSAGES, channel_problem, mirror_client
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.locks import job_lock
from frappe_slack_connector.slack.app import CHANNEL_ID_PATTERN, SlackIntegration

# A failed mirror counts as live: it is retried or stopped, never bypassed by a new
# send, so a failure cannot lead to a second Slack thread for the same email thread
LIVE_STATUSES = ("Active", "Failed")
START_LOCK_TIMEOUT = 10
STATUS_FIELDS = ["name", "status", "channel_name", "last_error", "retry_count"]


def _live_mirror(thread: str, fields: list[str]):
    rows = frappe.get_all(
        MIRROR, filters={"gmail_thread": thread, "status": ["in", LIVE_STATUSES]}, fields=fields, limit=1
    )
    return rows[0] if rows else None


def _resolve_channel_id(channel: str) -> str | None:
    """The Slack ID for a Slack Channel row; the row's own value when it is an ID, else looked up by name."""
    stored = frappe.db.get_value("Slack Channel", channel, "channel_id")
    if stored and CHANNEL_ID_PATTERN.match(stored):
        return stored
    return SlackIntegration().resolve_channel_id(stored or channel)


def _post_note(mirror, text: str, blocks: list[dict]) -> None:
    """A note in the mirror's Slack thread. Best effort: the change it announces has already been made."""
    try:
        mirror_client().chat_postMessage(
            channel=mirror.channel_id,
            thread_ts=mirror.thread_ts,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
    except Exception as exc:
        generate_error_log(
            "Gmail thread mirror: could not post a note in Slack", message=f"Mirror {mirror.name}", exception=exc
        )


@frappe.whitelist()
def get_status(thread: str) -> dict:
    """Whether mirroring is available, and the thread's mirror: the live one, else the last stopped one."""
    if not is_enabled():
        return {"enabled": False, "mirror": None}
    frappe.has_permission("Gmail Thread", "read", doc=thread, throw=True)
    mirror = _live_mirror(thread, STATUS_FIELDS)
    if not mirror:
        stopped = frappe.get_all(
            MIRROR,
            filters={"gmail_thread": thread, "status": "Stopped"},
            fields=STATUS_FIELDS,
            order_by="creation desc, name desc",
            limit=1,
        )
        mirror = stopped[0] if stopped else None
    return {"enabled": True, "mirror": dict(mirror) if mirror else None}


@frappe.whitelist(methods=["POST"])
def start(thread: str, channel: str) -> str:
    """
    Mirror the Gmail Thread into the given Slack Channel row; returns the mirror's name.
    A mirror of the same thread that was stopped in the same channel is continued in
    its Slack thread; otherwise a new Slack thread is started.
    """
    if not is_enabled():
        frappe.throw(_("Gmail thread mirroring is not enabled."))
    frappe.has_permission("Gmail Thread", "write", doc=thread, throw=True)
    if not frappe.db.exists("Slack Channel", channel):
        frappe.throw(_("Could not find Slack channel {0}.").format(channel))
    frappe.has_permission("Slack Channel", "read", doc=channel, throw=True)

    channel_id = _resolve_channel_id(channel)
    if not channel_id:
        frappe.throw(_("Could not find Slack channel {0}.").format(channel))
    problem = channel_problem(mirror_client(), channel_id)
    if problem:
        frappe.throw(_(PROBLEM_MESSAGES[problem]).format(channel))

    try:
        # Two clicks at once must not both pass the "already mirrored" check
        with job_lock(f"gmail_mirror_start_{thread}", START_LOCK_TIMEOUT):
            live = _live_mirror(thread, ["channel_name"])
            if live:
                frappe.throw(
                    _("This thread is already mirrored in #{0}. Stop that mirror first.").format(live.channel_name)
                )
            name = _resume(thread, channel, channel_id) or _create(thread, channel, channel_id)
    except LockTimeoutError:
        frappe.throw(_("Another request for this thread is in progress. Try again in a moment."))
    enqueue_post(name, after_commit=True)
    return name


def _create(thread: str, channel: str, channel_id: str) -> str:
    mirror = frappe.get_doc(
        {
            "doctype": MIRROR,
            "gmail_thread": thread,
            "channel_id": channel_id,
            "channel_name": channel,
            "status": "Active",
        }
    )
    mirror.insert(ignore_permissions=True)
    return mirror.name


def _resume(thread: str, channel: str, channel_id: str) -> str | None:
    """
    Reactivate the thread's last mirror in this channel, if it can be continued:
    it has a Slack thread and no recorded error. A mirror stopped while failed
    keeps its error, so Stop then Send again starts afresh when a Slack thread is gone.
    """
    rows = frappe.get_all(
        MIRROR,
        filters={"gmail_thread": thread, "channel_id": channel_id, "status": "Stopped"},
        fields=["name", "channel_id", "thread_ts", "last_error"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not rows or not rows[0].thread_ts or rows[0].last_error:
        return None
    mirror = rows[0]
    frappe.db.set_value(MIRROR, mirror.name, {"status": "Active", "retry_count": 0, "channel_name": channel})
    _post_note(mirror, RESUMED_TEXT, resumed_blocks(get_fullname(frappe.session.user)))
    return mirror.name


@frappe.whitelist(methods=["POST"])
def stop(thread: str) -> None:
    """Stop the thread's mirror. Messages already in Slack stay; a note says no more will follow."""
    frappe.has_permission("Gmail Thread", "write", doc=thread, throw=True)
    mirror = _live_mirror(thread, ["name", "channel_id", "thread_ts"])
    if not mirror:
        frappe.throw(_("This thread is not mirrored."))
    frappe.db.set_value(MIRROR, mirror.name, "status", "Stopped")
    if mirror.thread_ts:
        _post_note(mirror, STOPPED_TEXT, stopped_blocks(get_fullname(frappe.session.user)))


@frappe.whitelist(methods=["POST"])
def retry(thread: str) -> None:
    """Run a failed mirror again; it continues in its Slack thread from the first email not yet posted."""
    if not is_enabled():
        frappe.throw(_("Gmail thread mirroring is not enabled."))
    frappe.has_permission("Gmail Thread", "write", doc=thread, throw=True)
    name = frappe.db.get_value(MIRROR, {"gmail_thread": thread, "status": "Failed"})
    if not name:
        frappe.throw(_("There is nothing to retry."))
    frappe.db.set_value(MIRROR, name, {"status": "Active", "retry_count": 0, "last_error": ""})
    enqueue_post(name, after_commit=True)

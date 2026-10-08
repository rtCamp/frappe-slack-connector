from datetime import UTC, datetime

import frappe
from frappe.model.document import Document
from frappe.utils import convert_utc_to_system_timezone, get_url_to_form, getdate

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.helpers.str_utils import escape_slack_mrkdwn
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.tasks.attendance_summary import get_leave_type

# Custom field on Leave Application (fixtures/custom_field.json) holding the ts
# of the reply posted in the attendance summary thread for a same-day leave
ATTENDANCE_REPLY_TS_FIELD = "custom_slack_attendance_reply_ts"


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted
    """
    frappe.enqueue(
        send_leave_notification_bg,
        queue="short",
        doc=doc,
    )
    frappe.enqueue(
        send_leave_notification_to_applicant,
        queue="short",
        doc=doc,
    )


def send_leave_notification_to_applicant(doc: Document):
    # Send a confirmation message to the user
    slack = SlackIntegration()
    user_id = slack.get_slack_user_id(employee_id=doc.employee)
    slack.slack_app.client.chat_postMessage(
        channel=user_id,
        blocks=format_leave_submission_blocks(
            leave_id=doc.name,
            employee_name=doc.employee,
            leave_link=get_url_to_form("Leave Application", doc.name),
            leave_type=doc.leave_type,
            leave_submission_date=doc.creation,
            from_date=doc.from_date,
            user_slack=user_id,
            to_date=doc.to_date,
            reason=doc.description,
        ),
    )


def format_leave_submission_blocks(
    *,
    leave_id: str,
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    user_slack: str,
    from_date: str,
    to_date: str,
    reason: str,
    leave_link: str = "#",
) -> list:
    """
    Format the blocks for the leave application message
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":memo: Leave Request Submitted",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Hello<@{user_slack}>! Your leave request has been successfully submitted.",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Leave ID:* <{leave_link}|{leave_id}> • *Submitted On:* {standard_date_fmt(leave_submission_date)}",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*From:*\n:calendar: {standard_date_fmt(from_date)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Leave Type:*\n:rocket: {leave_type}",
                },
            ],
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*To:*\n:calendar: {standard_date_fmt(to_date)}",
                },
                {"type": "mrkdwn", "text": f"*Reason:*\n>{reason}"},
            ],
        },
    ]
    return blocks


def post_same_day_leave_to_attendance_thread(doc: Document, slack: SlackIntegration | None = None) -> str | None:
    """
    Reply in today's attendance summary thread when the leave covers today
    and the summary has already been posted (a leave applied for before the
    summary is simply included in the summary itself)

    The reply ``ts`` is stored on the Leave Application so the reply can be
    removed if the leave is rejected or cancelled later the same day.
    Returns the ``ts`` of the reply, or None when nothing was posted
    """
    # Data Import of backdated rows and leaves created already decided
    # (e.g. Rejected) are not announcements
    if frappe.flags.in_import or doc.status not in ("Open", "Approved"):
        return None

    today = getdate(frappe.utils.today())
    slack_settings = frappe.get_single("Slack Settings")
    if (
        slack_settings.send_attendance_updates != 1
        or not slack_settings.last_attendance_msg_ts
        or not slack_settings.last_attendance_date
        or getdate(slack_settings.last_attendance_date) != today
    ):
        return None

    # from_date/to_date may be strings or dates depending on how the doc was built
    if not (getdate(doc.from_date) <= today <= getdate(doc.to_date)):
        return None

    # Match the summary, which only lists active employees
    if frappe.db.get_value("Employee", doc.employee, "status") != "Active":
        return None

    slack = slack or SlackIntegration()
    user_slack = slack.get_slack_user_id(employee_id=doc.employee)
    name = f"<@{user_slack}>" if user_slack and slack_settings.mention_user else escape_slack_mrkdwn(doc.employee_name)
    day_period = get_leave_type(doc, on_date=today)

    response = slack.slack_app.client.chat_postMessage(
        channel=slack.SLACK_CHANNEL_ID,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{name} is on leave today. _({day_period})_",
                },
            },
        ],
        thread_ts=slack_settings.last_attendance_msg_ts,
        reply_broadcast=True,
    )
    reply_ts = response["ts"]
    frappe.db.set_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD, reply_ts, update_modified=False)
    return reply_ts


def send_leave_notification_bg(doc: Document):
    """
    Send a slack message to the leave approver when
    a new leave application is submitted

    Also send a notification to the attendance channel thread if
    the leave covers today and attendance notification is already sent
    """
    slack = SlackIntegration()
    try:
        approver_slack = slack.get_slack_user_id(user_email=doc.leave_approver)
    except Exception as e:
        generate_error_log(
            title="Error fetching approver slack id",
            exception=e,
        )
        approver_slack = None

    try:
        post_same_day_leave_to_attendance_thread(doc, slack=slack)
    except Exception as e:
        generate_error_log(
            title="Error posting same-day leave to attendance thread",
            exception=e,
        )

    try:
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name

        # Send message to approver
        if approver_slack is not None:
            slack.slack_app.client.chat_postMessage(
                channel=approver_slack,
                blocks=format_leave_application_blocks(
                    leave_id=doc.name,
                    leave_link=get_url_to_form("Leave Application", doc.name),
                    employee_name=mention,
                    leave_type=doc.leave_type,
                    is_half_day=doc.half_day,
                    leave_submission_date=standard_date_fmt(doc.creation),
                    from_date=standard_date_fmt(doc.from_date),
                    to_date=standard_date_fmt(doc.to_date),
                    reason=doc.description,
                ),
            )

    except Exception as e:
        generate_error_log(
            title="Error posting message to Slack",
            exception=e,
        )


def _reply_posted_today(reply_ts: str) -> bool:
    """
    Whether a Slack message ``ts`` falls on today's date in the site timezone
    """
    # A Slack ts is a Unix epoch in seconds (with a sequence suffix after the
    # dot), so read it as UTC and shift it into the site timezone before
    # comparing calendar dates
    posted_at = convert_utc_to_system_timezone(datetime.fromtimestamp(float(reply_ts), tz=UTC))
    return getdate(posted_at) == getdate(frappe.utils.today())


def restore_attendance_reply_ts(doc: Document, method=None):
    """
    Keep the stored reply ts from being erased by a stale save

    The ts is written by a background job with ``db.set_value`` after the
    doc was created, so a form loaded before that (or a client that sends
    the whole doc) carries an empty value, and ``db_update`` would write
    every column back. The database is the source of truth for this field:
    only the background jobs write or clear it, so copy it onto the doc
    whenever the in-memory value is empty
    """
    if doc.is_new() or doc.get(ATTENDANCE_REPLY_TS_FIELD):
        return

    stored_ts = frappe.db.get_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD)
    if stored_ts:
        doc.set(ATTENDANCE_REPLY_TS_FIELD, stored_ts)


def _enqueue_attendance_reply_removal(doc: Document):
    """
    Enqueue the removal of the attendance thread reply stored on the leave
    if it was posted today. A reply from an earlier day is history and is
    left alone. Never raises: a bad ts or a queue outage must not block the
    user's action
    """
    try:
        reply_ts = doc.get(ATTENDANCE_REPLY_TS_FIELD)
        if not reply_ts or not _reply_posted_today(reply_ts):
            return

        # Only delete once the status change is committed: on_update runs
        # before on_submit, which can still throw and roll back
        frappe.enqueue(
            remove_attendance_reply_bg,
            queue="short",
            enqueue_after_commit=True,
            doc=doc,
        )
    except Exception as e:
        generate_error_log(
            title="Error scheduling attendance thread reply removal",
            exception=e,
        )


def on_update_remove_attendance_reply(doc: Document, method=None):
    """
    Remove the same-day attendance thread reply when the leave is rejected
    or cancelled on the day the reply was posted

    Wired to ``on_update``, ``on_update_after_submit``, ``on_cancel`` and
    ``on_discard``: Frappe only runs ``on_cancel`` for a cancel, HRMS sets
    status to Cancelled in ``before_cancel`` and ``on_discard``, and a
    workflow may move a submitted leave to Rejected
    """
    if not doc.has_value_changed("status") or doc.status not in ("Rejected", "Cancelled"):
        return

    _enqueue_attendance_reply_removal(doc)


def on_trash_remove_attendance_reply(doc: Document, method=None):
    """
    Remove the same-day attendance thread reply when the leave is deleted
    (deleting a draft runs neither ``on_update`` nor ``on_cancel``)
    """
    _enqueue_attendance_reply_removal(doc)


def remove_attendance_reply_bg(doc: Document):
    """
    Delete the attendance thread reply stored on the leave and clear the
    stored ts. Deleting a broadcast reply removes it from the thread and
    the channel
    """
    reply_ts = doc.get(ATTENDANCE_REPLY_TS_FIELD)
    if not reply_ts:
        return

    try:
        slack = SlackIntegration()
        slack.slack_app.client.chat_delete(channel=slack.SLACK_CHANNEL_ID, ts=reply_ts)
    except Exception as e:
        # The reply may already be gone (deleted by hand); log and move on
        generate_error_log(
            title="Error deleting attendance thread reply",
            exception=e,
        )

    # The ts is only useful on the day it was posted, so clear it either way
    frappe.db.set_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD, None, update_modified=False)


def format_leave_application_blocks(
    *,
    leave_id: str,
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    from_date: str,
    to_date: str,
    is_half_day: bool,
    reason: str = "",
    employee_link: str = "#",
    leave_link: str = "#",
) -> list:
    """
    Format the blocks for the leave application message
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":memo: New Leave Application",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{employee_name} has submitted a new leave request.",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Leave ID:* <{leave_link}|{leave_id}> ",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Leave Type:*\n:rocket: {leave_type}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Submitted On:*\n:clock3: {leave_submission_date}",
                },
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n:date: {from_date}"},
                {"type": "mrkdwn", "text": f"*To:*\n:date: {to_date}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reason:*\n>{reason if reason else 'No reason provided'}",
            },
        },
    ]

    # Add a context menu indicating it is a half day
    if is_half_day:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Half Day: :white_check_mark:",
                    }
                ],
            }
        )

    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": "leave_actions_block",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "emoji": True, "text": "Approve"},
                        "style": "primary",
                        "value": leave_id,
                        "action_id": "leave_approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "emoji": True, "text": "Reject"},
                        "style": "danger",
                        "value": leave_id,
                        "action_id": "leave_reject",
                    },
                ],
            },
            {
                "type": "context",
                "block_id": "footer_block",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Please review and take action on this leave request.",
                    }
                ],
            },
        ]
    )
    return blocks

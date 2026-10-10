from datetime import UTC, datetime

import frappe
from frappe.model.document import Document
from frappe.utils import convert_utc_to_system_timezone, get_url_to_form, getdate

from frappe_slack_connector.db.employee import get_employees_on_holiday
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.helpers.str_utils import escape_slack_text
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.tasks.attendance_summary import get_leave_type

# Custom field on Leave Application (fixtures/custom_field.json) holding the ts
# of the reply posted in the attendance summary thread for a same-day leave
ATTENDANCE_REPLY_TS_FIELD = "custom_slack_attendance_reply_ts"

# Appended to a struck-through thread reply. One word for every path
# (rejected, cancelled, discarded, deleted): the channel only needs to know
# the leave is off, not who called it off
WITHDRAWN_LABEL = "Cancelled"


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted
    """
    # Rows created by Data Import are not announcements. Decide here: the
    # background job runs with fresh frappe.local, where the flag is reset.
    # Both jobs wait for the commit: the thread reply writes its ts back to
    # this row, which a fast worker could otherwise miss (or announce a leave
    # whose insert then rolls back)
    frappe.enqueue(
        send_leave_notification_bg,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
        announce_in_thread=not frappe.flags.in_import,
    )
    frappe.enqueue(
        send_leave_notification_to_applicant,
        queue="short",
        enqueue_after_commit=True,
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


def _same_day_reply_name(slack: SlackIntegration, employee: str, employee_name: str, mention_user) -> str:
    """Mention when the employee has a Slack id and mentions are on, else the escaped name"""
    user_slack = slack.get_slack_user_id(employee_id=employee)
    return f"<@{user_slack}>" if user_slack and mention_user else escape_slack_text(employee_name)


def _same_day_reply_text(name: str, day_period: str) -> str:
    return f"{name} is on leave today. _({day_period})_"


def _same_day_reply_block(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def post_same_day_leave_to_attendance_thread(doc: Document, slack: SlackIntegration | None = None) -> str | None:
    """
    Reply in today's attendance summary thread when the leave covers today
    and the summary has already been posted (a leave applied for before the
    summary is simply included in the summary itself)

    The reply ``ts`` is stored on the Leave Application so the reply can be
    removed if the leave is rejected or cancelled later the same day.
    Returns the ``ts`` of the reply, or None when nothing was posted
    """
    # A leave created already decided (e.g. Rejected) is not an announcement
    if doc.status not in ("Open", "Approved"):
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

    # Same rule as the summary: a leave spanning today is only "leave" when
    # today is a working day on the employee's own holiday list
    if doc.employee in get_employees_on_holiday([doc.employee], today):
        return None

    slack = slack or SlackIntegration()
    name = _same_day_reply_name(slack, doc.employee, doc.employee_name, slack_settings.mention_user)
    day_period = get_leave_type(doc, on_date=today)

    response = slack.slack_app.client.chat_postMessage(
        channel=slack.SLACK_CHANNEL_ID,
        blocks=[_same_day_reply_block(_same_day_reply_text(name, day_period))],
        thread_ts=slack_settings.last_attendance_msg_ts,
        reply_broadcast=True,
    )
    reply_ts = response["ts"]
    frappe.db.set_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD, reply_ts, update_modified=False)
    return reply_ts


def send_leave_notification_bg(doc: Document, announce_in_thread: bool = True):
    """
    Send a slack message to the leave approver when
    a new leave application is submitted

    Also send a notification to the attendance channel thread if
    the leave covers today and attendance notification is already sent
    (skipped when ``announce_in_thread`` is False, e.g. for imported rows)
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

    if announce_in_thread:
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
    Keep the stored reply ts server-owned

    The field is hidden and read-only, but Frappe does not enforce
    ``read_only`` on API writes, and the bot deletes whatever ts is stored
    here. Only the background jobs may write or clear it with
    ``db.set_value``, so the value a client sends is never trusted: a new
    doc gets it blanked, and an existing doc gets the database value put
    back (this also stops a form loaded before the job ran from erasing it
    with ``db_update``, which writes every column)
    """
    # is_new() only knows about docs going through insert(); a doc built in
    # memory and never inserted has no name either
    if doc.is_new() or not doc.name:
        doc.set(ATTENDANCE_REPLY_TS_FIELD, None)
        return

    doc.set(
        ATTENDANCE_REPLY_TS_FIELD,
        frappe.db.get_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD),
    )


def _enqueue_attendance_reply_withdrawal(doc: Document):
    """
    Enqueue marking the attendance thread reply stored on the leave as
    withdrawn, if it was posted today. A reply from an earlier day is history
    and is left alone. Never raises: a bad ts or a queue outage must not
    block the user's action
    """
    try:
        # Read the stored value, never the one on the doc (see
        # restore_attendance_reply_ts), and hand everything the job needs to
        # it so it does not re-read a row that may be gone by then (on_trash)
        reply_ts = frappe.db.get_value("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD)
        if not reply_ts or not _reply_posted_today(reply_ts):
            return

        # Only edit once the status change is committed: on_update runs
        # before on_submit, which can still throw and roll back
        frappe.enqueue(
            withdraw_attendance_reply_bg,
            queue="short",
            enqueue_after_commit=True,
            leave_name=doc.name,
            reply_ts=reply_ts,
            employee=doc.employee,
            employee_name=doc.employee_name,
            day_period=get_leave_type(doc, on_date=getdate(frappe.utils.today())),
        )
    except Exception as e:
        generate_error_log(
            title="Error scheduling attendance thread reply withdrawal",
            exception=e,
        )


def on_update_withdraw_attendance_reply(doc: Document, method=None):
    """
    Strike through the same-day attendance thread reply when the leave is
    rejected or cancelled on the day the reply was posted. The reply stays
    in the thread as history, marked "(Cancelled)" either way

    Wired to ``on_update``, ``on_update_after_submit``, ``on_cancel`` and
    ``on_discard``: Frappe only runs ``on_cancel`` for a cancel, HRMS sets
    status to Cancelled in ``before_cancel`` and ``on_discard``, and a
    workflow may move a submitted leave to Rejected
    """
    if not doc.has_value_changed("status") or doc.status not in ("Rejected", "Cancelled"):
        return

    _enqueue_attendance_reply_withdrawal(doc)


def on_trash_withdraw_attendance_reply(doc: Document, method=None):
    """
    Strike through the same-day attendance thread reply when the leave is
    deleted (deleting a draft runs neither ``on_update`` nor ``on_cancel``)
    """
    _enqueue_attendance_reply_withdrawal(doc)


def withdraw_attendance_reply_bg(
    leave_name: str,
    reply_ts: str,
    *,
    employee: str,
    employee_name: str,
    day_period: str,
):
    """
    Edit the attendance thread reply ``reply_ts`` posted for the leave
    ``leave_name`` so it reads struck through with "(Cancelled)" appended, and
    clear the stored ts. Nothing is deleted: the thread keeps a record of
    the leave that was announced and then taken back. Editing a broadcast
    reply changes it in the thread and in the channel
    """
    if not reply_ts:
        return

    # The bot must only ever edit its own thread replies, never the summary
    # message the thread hangs off
    if reply_ts == frappe.db.get_single_value("Slack Settings", "last_attendance_msg_ts"):
        generate_error_log(
            title="Refused to edit the attendance summary",
            message=f"Leave Application {leave_name} stores the summary ts {reply_ts} as its reply ts",
        )
        return

    try:
        slack = SlackIntegration()
        mention_user = frappe.db.get_single_value("Slack Settings", "mention_user")
        name = _same_day_reply_name(slack, employee, employee_name, mention_user)
        text = f"~{_same_day_reply_text(name, day_period)}~ ({WITHDRAWN_LABEL})"
        slack.slack_app.client.chat_update(
            channel=slack.SLACK_CHANNEL_ID,
            ts=reply_ts,
            blocks=[_same_day_reply_block(text)],
            text=f"{escape_slack_text(employee_name)} is on leave today ({WITHDRAWN_LABEL})",
        )
    except Exception as e:
        # The reply may already be gone (deleted by hand); log and move on
        generate_error_log(
            title="Error marking attendance thread reply as withdrawn",
            exception=e,
        )

    # The ts is only useful on the day it was posted, so clear it either way
    # (a no-op when the leave itself has been deleted)
    frappe.db.set_value("Leave Application", leave_name, ATTENDANCE_REPLY_TS_FIELD, None, update_modified=False)


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

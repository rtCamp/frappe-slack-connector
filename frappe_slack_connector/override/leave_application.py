import frappe
from frappe.model.document import Document
from frappe.utils import get_url_to_form, getdate, today

from frappe_slack_connector.db.leave_application import custom_fields_exist
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.tasks.attendance_summary import update_attendance_summary

# Statuses counted in the attendance summary, and those that take a leave out of it
SUMMARY_INCLUDED_STATUSES = ("Open", "Approved")
SUMMARY_EXCLUDED_STATUSES = ("Rejected", "Cancelled")


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
    # A leave applied for after the morning post changes today's count
    enqueue_attendance_summary_refresh(doc)


def on_update_refresh_attendance_summary(doc, method=None):
    """
    Refresh today's attendance summary when a leave covering today is
    rejected or cancelled after the summary has been posted

    Wired to ``on_update`` (Desk and Slack rejection, with or without a
    workflow), ``on_cancel`` (submitted leave cancelled) and ``on_discard``
    (draft leave discarded). Only fires when the status actually changed
    from one the summary counts, so edits to an already-rejected leave and
    Rejected -> Cancelled transitions do not rebuild the summary.
    """
    if doc.status not in SUMMARY_EXCLUDED_STATUSES or not doc.has_value_changed("status"):
        return
    previous_status = doc.get_value_before_save("status")
    # No before-save copy means we cannot tell; refresh rather than go stale
    if previous_status is not None and previous_status not in SUMMARY_INCLUDED_STATUSES:
        return
    enqueue_attendance_summary_refresh(doc)


def should_refresh_attendance_summary(doc) -> bool:
    """
    Whether a change to this leave affects today's posted attendance summary
    True only when attendance updates are enabled, today's summary has
    already been posted and the leave covers today. If the summary has not
    been posted yet it will reflect the current state on its own.
    """
    slack_settings = frappe.get_single("Slack Settings")
    if (
        slack_settings.send_attendance_updates != 1
        or not slack_settings.last_attendance_msg_ts
        or not slack_settings.last_attendance_date
    ):
        return False

    current_date = getdate(today())
    if getdate(slack_settings.last_attendance_date) != current_date:
        return False

    return getdate(doc.from_date) <= current_date <= getdate(doc.to_date)


def enqueue_attendance_summary_refresh(doc) -> None:
    """
    Enqueue the in-place rebuild of today's attendance summary if the
    leave affects it

    The job reads the Leave Application table, so it is queued after the
    current transaction commits; otherwise the worker could rebuild the
    summary from the state before this change.
    """
    if not should_refresh_attendance_summary(doc):
        return
    frappe.enqueue(
        update_attendance_summary,
        queue="short",
        enqueue_after_commit=True,
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


def send_leave_notification_bg(doc: Document):
    """
    Send a slack message to the leave approver when
    a new leave application is submitted

    Also send a notification to the attendance channel thread if
    the leave date is today and attendance notification is already sent
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
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention_users = frappe.db.get_single_value("Slack Settings", "mention_user")
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name
        day_period = "Full Day"
        if doc.half_day and doc.half_day_date == frappe.utils.today():
            day_period = doc.custom_first_halfsecond_half if custom_fields_exist() else "Half Day"

        # if leave date is today and attendance notification is already sent,
        # send notification to attendance channel thread
        slack_settings = frappe.get_single("Slack Settings")
        if (
            doc.from_date == frappe.utils.today()
            and slack_settings.send_attendance_updates == 1
            and slack_settings.last_attendance_date is not None
            and slack_settings.last_attendance_msg_ts is not None
            and slack_settings.last_attendance_date == frappe.utils.nowdate()
        ):
            slack.slack_app.client.chat_postMessage(
                channel=slack.SLACK_CHANNEL_ID,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{mention if mention_users else doc.employee_name} requested for leave today. "
                            + f"_({day_period})_",
                        },
                    },
                ],
                thread_ts=slack_settings.last_attendance_msg_ts,
                reply_broadcast=True,
            )

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

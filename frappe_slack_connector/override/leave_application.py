import frappe
from frappe.model.document import Document
from frappe.utils import get_url_to_form

from frappe_slack_connector.db.leave_application import (
    custom_fields_exist,
    get_applicant_message_ref,
    store_applicant_message_ref,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


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


# Leave statuses that count as a decision on the request, and the header the
# applicant's DM is rewritten with when the leave reaches that status
APPLICANT_DECISION_HEADERS = {
    "Approved": ":white_check_mark: Leave Request Approved",
    "Rejected": ":x: Leave Request Rejected",
    "Cancelled": ":no_entry_sign: Leave Request Cancelled",
}


def on_update_notify_applicant(doc: Document, method: str | None = None):
    """
    Update the applicant's Slack DM when the leave is approved, rejected or
    cancelled, whether the decision was taken in Desk or from Slack.

    Hooked to ``on_update`` (approve/reject go through save/submit) and
    ``on_cancel`` (HRMS sets status to Cancelled in ``before_cancel``, and a
    cancel does not fire ``on_update``).
    """
    if doc.status not in APPLICANT_DECISION_HEADERS or not doc.has_value_changed("status"):
        return
    # Approval and rejection are the approver's decision; a cancellation may
    # come from HR or the applicant, so report whoever performed it
    decided_by = doc.modified_by if doc.status == "Cancelled" else doc.leave_approver
    frappe.enqueue(
        send_leave_decision_to_applicant,
        queue="short",
        doc=doc,
        status=doc.status,
        decided_by=decided_by,
    )


def send_leave_notification_to_applicant(doc: Document):
    """
    Send a confirmation message to the applicant and remember the message
    so it can be updated in place once a decision is taken
    """
    try:
        slack = SlackIntegration()
        user_id = slack.get_slack_user_id(employee_id=doc.employee)
        if not user_id:
            generate_error_log(
                title="Applicant Slack ID not found",
                message=f"No Slack user found for employee {doc.employee} (Leave Application {doc.name})",
            )
            return
        response = slack.slack_app.client.chat_postMessage(
            channel=user_id,
            blocks=_applicant_blocks(doc, user_id),
        )
        store_applicant_message_ref(doc.name, channel=response.get("channel"), ts=response.get("ts"))
    except Exception as e:
        generate_error_log(
            title="Error posting leave confirmation to applicant",
            exception=e,
        )


def send_leave_decision_to_applicant(doc: Document, status: str, decided_by: str | None = None):
    """
    Rewrite the applicant's "Leave Request Submitted" DM with the decision.

    Falls back to posting a fresh DM when no message reference is stored
    (leaves created before the reference was tracked, or whose first DM
    failed to post).
    """
    try:
        slack = SlackIntegration()
        user_id = slack.get_slack_user_id(employee_id=doc.employee)
        if not user_id:
            generate_error_log(
                title="Applicant Slack ID not found",
                message=f"No Slack user found for employee {doc.employee} (Leave Application {doc.name})",
            )
            return
        blocks = _applicant_blocks(
            doc,
            user_id,
            status=status,
            status_by=_format_decider(slack, decided_by),
        )
        # Read the reference from the database rather than from ``doc``: the
        # job receives a snapshot taken before the submission DM was stored
        channel, ts = get_applicant_message_ref(doc.name)
        if channel and ts:
            slack.slack_app.client.chat_update(channel=channel, ts=ts, blocks=blocks)
            return
        response = slack.slack_app.client.chat_postMessage(channel=user_id, blocks=blocks)
        store_applicant_message_ref(doc.name, channel=response.get("channel"), ts=response.get("ts"))
    except Exception as e:
        generate_error_log(
            title="Error updating leave decision for applicant",
            exception=e,
        )


def _format_decider(slack: SlackIntegration, user_email: str | None) -> str | None:
    """
    Render the user who took the decision: a Slack mention when mentions are
    enabled and the user has a Slack ID, otherwise their full name
    """
    if not user_email:
        return None
    try:
        slack_id = slack.get_slack_user_id(user_email=user_email)
    except Exception as e:
        generate_error_log(
            title="Error fetching approver slack id",
            exception=e,
        )
        slack_id = None
    if slack_id and frappe.db.get_single_value("Slack Settings", "mention_user"):
        return f"<@{slack_id}>"
    return frappe.db.get_value("User", user_email, "full_name") or user_email


def _applicant_blocks(doc: Document, user_slack: str, *, status: str | None = None, status_by: str | None = None):
    """
    Build the applicant DM blocks for the given leave, optionally with a decision
    """
    return format_leave_submission_blocks(
        leave_id=doc.name,
        employee_name=doc.employee,
        leave_link=get_url_to_form("Leave Application", doc.name),
        leave_type=doc.leave_type,
        leave_submission_date=doc.creation,
        from_date=doc.from_date,
        user_slack=user_slack,
        to_date=doc.to_date,
        reason=doc.description,
        status=status,
        status_by=status_by,
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
    status: str | None = None,
    status_by: str | None = None,
) -> list:
    """
    Format the blocks for the leave application message.

    With ``status`` (Approved, Rejected or Cancelled) the header reflects the
    decision and a status line, naming ``status_by`` when given, is appended.
    """
    if status:
        header = APPLICANT_DECISION_HEADERS[status]
        intro = f"Hello<@{user_slack}>! Your leave request has been {status.lower()}."
    else:
        header = ":memo: Leave Request Submitted"
        intro = f"Hello<@{user_slack}>! Your leave request has been successfully submitted."
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": intro,
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
    if status:
        status_text = f"{status} by {status_by}" if status_by else status
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Status:* {status_text}"},
                },
            ]
        )
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

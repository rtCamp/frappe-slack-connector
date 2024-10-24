import frappe
from frappe.model.document import Document
from frappe.utils import get_url_to_form

from frappe_slack_connector.db.leave_application import custom_fields_exist
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
                            "text": f"{mention} requested for leave today. " + f"_({day_period})_",
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
    return blocks

import frappe
from frappe.model.document import Document

from slack_connector.helpers.error import generate_error_log
from slack_connector.helpers.standard_date import standard_date_fmt
from slack_connector.slack.app import SlackIntegration


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
    Send a slack message to the leave approver when a new leave application
    is submitted
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
    if approver_slack is None:
        return

    try:
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name

        # TODO: Add CC users (override from `rtcamp` app)
        # if hasattr(doc, "custom_notify_users"):
        #     alert_message += f"CC: {', '.join([user_doc.user for user_doc in doc.custom_notify_users])}\n"

        slack.slack_app.client.chat_postMessage(
            channel=approver_slack,
            blocks=format_leave_application_blocks(
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
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    from_date: str,
    to_date: str,
    reason: str = None,
    employee_link: str = "#",
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
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "emoji": True, "text": "Approve"},
                    "style": "primary",
                    "value": "approve_leave",
                    "action_id": "approve_leave",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "emoji": True, "text": "Reject"},
                    "style": "danger",
                    "value": "reject_leave",
                    "action_id": "reject_leave",
                },
                # {
                #     "type": "button",
                #     "text": {
                #         "type": "plain_text",
                #         "emoji": True,
                #         "text": "View Details",
                #     },
                #     "value": "view_details",
                #     "action_id": "view_leave_details",
                # },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Please review and take action on this leave request.",
                }
            ],
        },
    ]
    return blocks

import frappe

from slack_connector.helpers.standard_date import standard_date_fmt
from slack_connector.slack.app import SlackIntegration


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted
    """
    slack = SlackIntegration()
    try:
        approver_slack = slack.get_slack_user_id(user_email=doc.leave_approver)
    except Exception as e:
        frappe.log_error(title="Error fetching approver slack id", message=str(e))
        approver_slack = None
    if approver_slack is None:
        return
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
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*You have a new Leave Application request:*\n_Requested by:_ {employee_name}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Type:* {leave_type}"},
                {
                    "type": "mrkdwn",
                    "text": f"*When:* Submitted {leave_submission_date}",
                },
                {"type": "mrkdwn", "text": f"*From:* {from_date}"},
                {"type": "mrkdwn", "text": f"*Reason:* {reason}"},
                {"type": "mrkdwn", "text": f"*To:* {to_date}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "emoji": True, "text": "Approve"},
                    "style": "primary",
                    "value": "click_me_123",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "emoji": True, "text": "Reject"},
                    "style": "danger",
                    "value": "click_me_123",
                },
            ],
        },
    ]
    return blocks

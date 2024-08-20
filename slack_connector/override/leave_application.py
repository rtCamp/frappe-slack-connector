import frappe

from slack_connector.helpers.slack_app import slack_app
from slack_connector.helpers.slack_methods import get_slack_user_id


def after_insert(doc, method):
    user = get_slack_user_id(doc.leave_approver)
    alert_message = (
        f"*New leave Application:*\n"
        f"*Applied by:* _{doc.employee_name}_\n"
        f"*Leave Type:* _{doc.leave_type}_\n"
        f"*Date:* _{frappe.utils.formatdate(doc.from_date)}_ to _{frappe.utils.formatdate(doc.to_date)}_\n"
        f"*Reason:* _{doc.description}_\n"
        f"*Approver:* _{doc.leave_approver_name}_\n"
    )
    if hasattr(doc, "custom_notify_users"):
        alert_message += (
            f"CC: {', '.join([user_doc.user for user_doc in doc.custom_notify_users])}\n"
        )
    slack_app.client.chat_postMessage(
        channel=user,
        text=alert_message,
    )

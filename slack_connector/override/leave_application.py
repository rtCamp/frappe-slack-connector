import frappe

from slack_connector.slack.app import SlackIntegration


def after_insert(doc, method):
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
    alert_message = (
        f"*New leave Application:*\n"
        f"*Applied by:* {mention}\n"
        f"*Leave Type:* _{doc.leave_type}_\n"
        f"*Date:* _{frappe.utils.formatdate(doc.from_date)}_ to _{frappe.utils.formatdate(doc.to_date)}_\n"
        f"*Reason:* _{doc.description}_\n"
    )
    if hasattr(doc, "custom_notify_users"):
        alert_message += f"CC: {', '.join([user_doc.user for user_doc in doc.custom_notify_users])}\n"
    slack.slack_app.client.chat_postMessage(
        channel=approver_slack,
        text=alert_message,
    )

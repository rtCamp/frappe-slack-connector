import frappe

from slack_connector.helpers.slack_app import slack_app


def after_insert(doc, method):
    approver_slack = frappe.db.get_value(
        "User Meta", {"user": doc.leave_approver}, "custom_username"
    )
    employee_email = frappe.get_value("Employee", doc.employee, "user_id")
    user_slack = frappe.db.get_value(
        "User Meta", {"user": employee_email}, "custom_username"
    )
    alert_message = (
        f"*New leave Application:*\n"
        f"*Applied by:* <@{user_slack}>\n"
        f"*Leave Type:* _{doc.leave_type}_\n"
        f"*Date:* _{frappe.utils.formatdate(doc.from_date)}_ to _{frappe.utils.formatdate(doc.to_date)}_\n"
        f"*Reason:* _{doc.description}_\n"
    )
    if hasattr(doc, "custom_notify_users"):
        alert_message += f"CC: {', '.join([user_doc.user for user_doc in doc.custom_notify_users])}\n"
    slack_app.client.chat_postMessage(
        channel=approver_slack,
        text=alert_message,
    )

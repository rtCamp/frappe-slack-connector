import frappe

from slack_connector.helpers.slack_app import slack_app
from slack_connector.helpers.slack_methods import get_employees_on_leave


def submit(doc, method):
    attendance_channel()
    frappe.throw("Testing Attendance Channel Notification")


def after_insert(doc, method):
    approver_slack = frappe.db.get_value(
        "User Meta", {"user": doc.leave_approver}, "custom_username"
    )
    # Approver is not connected to slack
    if approver_slack is None:
        return
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


def attendance_channel() -> None:
    announcement = "*People on Leave Today*"
    users_on_leave = get_employees_on_leave()
    for user_application in users_on_leave:
        user = frappe.get_value("Employee", user_application.employee, "user_id")
        user_slack = frappe.db.get_value("User Meta", {"user": user}, "custom_username")
        slack_name = (
            f"<@{user_slack}>" if user_slack else user_application.employee_name
        )
        announcement += f"\n{slack_name} is on leave from _{frappe.utils.formatdate(user_application.from_date)}_ to _{frappe.utils.formatdate(user_application.to_date)}_ ({user_application.status})"

    attendance_channel_id = frappe.db.get_single_value(
        "Slack Settings", "attendance_channel_id"
    )
    if attendance_channel_id:
        slack_app.client.chat_postMessage(
            channel=attendance_channel_id, text=announcement
        )

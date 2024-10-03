import frappe

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration


def on_update(doc, method):
    """
    Send a slack message to the timesheet approver when a new timesheet
    is submitted for approval.
    """
    if doc.custom_weekly_approval_status == "Approval Pending":
        frappe.enqueue(
            send_timesheet_approval_notification_bg,
            queue="short",
            doc=doc,
        )


def send_timesheet_approval_notification_bg(doc):
    """
    Send a slack message to the timesheet approver when a new timesheet
    is submitted for approval.
    """
    slack = SlackIntegration()

    reporting_manager = frappe.get_value("Employee", doc.employee, "reports_to")
    if not reporting_manager:
        generate_error_log(
            title=f"Reporting Manager for {doc.employee} not found",
            message="Slack notification not sent.",
        )
        return

    manager_slack = slack.get_slack_user_id(employee_id=reporting_manager)
    # approver not found, return
    if manager_slack is None:
        generate_error_log(
            title="Slack ID not found",
            message=f"Slack ID for {reporting_manager} not found. Slack notification not sent.",
        )
        return

    try:
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name
        message = f"Timesheet for {mention} is submitted for approval."
        slack.slack_app.client.chat_postMessage(
            channel=manager_slack,
            text=message,
        )
    except Exception as e:
        generate_error_log(
            title="Error sending timesheet submission notification",
            exception=e,
        )

import frappe

from slack_connector.db.leave_application import get_employees_on_leave
from slack_connector.db.user_meta import get_user_meta
from slack_connector.slack.app import SlackIntegration


def attendance_channel() -> None:
    slack = SlackIntegration()
    announcement = "*People on Leave Today*"
    users_on_leave = get_employees_on_leave()

    for user_application in users_on_leave:
        user_slack = get_user_meta(employee_id=user_application.get("employee"))
        slack_name = (
            f"<@{user_slack.custom_username}>"
            if user_slack
            else user_application.employee_name
        )
        announcement += (
            f"\n{slack_name} is on leave from "
            f"_{frappe.utils.formatdate(user_application.from_date)}_ to "
            f"_{frappe.utils.formatdate(user_application.to_date)}_ ({user_application.status})"
        )

    slack.slack_app.client.chat_postMessage(
        channel=slack.SLACK_CHANNEL_ID, text=announcement
    )

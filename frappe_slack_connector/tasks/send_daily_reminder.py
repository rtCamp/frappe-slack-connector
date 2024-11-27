import time

import frappe
from frappe.utils import add_days, get_time, getdate

from frappe_slack_connector.db.employee import check_if_date_is_holiday
from frappe_slack_connector.db.timesheet import get_employee_daily_working_norm, get_reported_time_by_employee
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


def send_reminder():
    """
    Send a reminder to the employees who have not made their daily
    time entries on the previous day
    Conditions:
     - Check if reminder is enabled
     - Check if last notification date is not the current date
    """
    slack_settings = frappe.get_single("Slack Settings")
    if (
        not slack_settings.timesheet_previousday_reminder
        or (
            slack_settings.last_timesheet_notification_date is not None
            and slack_settings.last_timesheet_notification_date == frappe.utils.nowdate()
        )
        or frappe.utils.now_datetime().time() < get_time(slack_settings.timesheet_daily_notification_time)
    ):
        return

    send_slack_notification(slack_settings.reminder_template, slack_settings.allowed_departments)

    slack_settings.last_timesheet_notification_date = frappe.utils.nowdate()
    slack_settings.save(ignore_permissions=True)


def send_slack_notification(reminder_template: str, allowed_departments: list):
    """
    Send the notification to the Slack users
    """
    slack = SlackIntegration()
    current_date = getdate()
    date = add_days(current_date, -1)

    reminder_template = frappe.get_doc("Email Template", reminder_template)
    allowed_departments = [doc.department for doc in allowed_departments]
    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active", "department": ["in", allowed_departments]},
        fields="*",
    )

    for employee in employees:
        user_slack = slack.get_slack_user_id(employee_id=employee)
        if not user_slack:
            continue
        if check_if_date_is_holiday(date, employee.name):
            continue

        daily_norm = get_employee_daily_working_norm(employee.name)

        # check if the employee has taken a half-day
        # and set the daily norm accordingly
        half_days = frappe.get_all(
            "Leave Application",
            {
                "employee": employee.name,
                "from_date": ("<=", str(date)),
                "to_date": (">=", str(date)),
                "half_day": 1,
                "status": (
                    "in",
                    ["Open", "Approved"],
                ),
            },
        )
        # if half day is taken for both the first and second half of the day,
        # then consider full day leave
        if len(half_days) > 1:
            continue
        elif half_days:
            daily_norm = daily_norm / 2

        hour = get_reported_time_by_employee(employee.name, date)
        if hour >= daily_norm:
            continue

        # Send slack notification to user
        try:
            args = {
                "date": standard_date_fmt(date),
                "name": employee.employee_name,
                "logged_time": hour,
                "mention": f"<@{user_slack}>",
                "daily_norm": daily_norm,
            }
            message = frappe.render_template(reminder_template.response_html, args)
            slack.slack_app.client.chat_postMessage(
                channel=user_slack,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": message,
                        },
                    },
                    {
                        "type": "divider",
                    },
                    {
                        "type": "actions",
                        "block_id": "daily_reminder_button",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Log Time",
                                },
                                "style": "primary",
                            },
                        ],
                    },
                ],
            )
        except Exception as e:
            generate_error_log(
                title="Error sending slack message",
                exception=e,
            )

        # NOTE:  Sleep for 1 second to avoid rate limiting
        time.sleep(1)

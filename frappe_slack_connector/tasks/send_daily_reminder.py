import frappe
from frappe.utils import add_days, getdate

from frappe_slack_connector.db.employee import check_if_date_is_holiday
from frappe_slack_connector.db.timesheet import get_employee_daily_working_norm, get_reported_time_by_employee
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


def send_reminder():
    """
    Send a reminder to the employees who have not made their daily
    time entries on the previous day
    """
    slack_settings = frappe.get_single("Slack Settings")
    if not slack_settings.timesheet_previousday_reminder:
        return

    slack = SlackIntegration()
    current_date = getdate()
    date = add_days(current_date, -1)

    reminder_template = frappe.get_doc("Email Template", slack_settings.reminder_template)
    allowed_departments = [doc.department for doc in slack_settings.allowed_departments]
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
        is_half_day = frappe.db.exists(
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
        if is_half_day:
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

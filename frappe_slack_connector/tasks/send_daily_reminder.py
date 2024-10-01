import frappe
from frappe.utils import add_days, getdate

from frappe_slack_connector.db.employee import check_if_date_is_holiday
from frappe_slack_connector.db.timesheet import (
    get_employee_daily_working_norm,
    get_reported_time_by_employee,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


def send_reminder():
    """
    Send a reminder to the employees who have not made their daily
    time entries on the previous day
    """
    slack = SlackIntegration()

    current_date = getdate()
    date = add_days(current_date, -1)

    reminder_template_name = frappe.db.get_single_value(
        fieldname="reminder_template", doctype="Slack Settings"
    )

    reminder_template = frappe.get_doc("Email Template", reminder_template_name)
    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        fields="*",
    )
    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        fields="*",
    )

    for employee in employees:
        user_slack = slack.get_slack_user_id(employee_id=employee)
        if not user_slack:
            continue

        if check_if_date_is_holiday(date, employee.name):
            continue
        daily_norm = get_employee_daily_working_norm(employee.name)
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
                ],
            )
        except Exception as e:
            generate_error_log(
                title="Error sending slack message",
                exception=e,
            )

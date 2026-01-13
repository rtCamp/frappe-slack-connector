import time

import frappe
from frappe.utils import add_days, get_time, getdate
from hrms.hr.utils import get_holiday_list_for_employee

from frappe_slack_connector.db.timesheet import is_next_pms_installed
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
    
    # Cache HR Settings standard working hours to avoid repeated queries
    standard_working_hours = frappe.db.get_single_value("HR Settings", "standard_working_hours") or 8
    
    # Determine fields to fetch based on installed apps
    employee_fields = ["name", "employee_name", "user_id", "holiday_list"]
    if is_next_pms_installed():
        employee_fields.extend(["custom_working_hours", "custom_work_schedule"])
    
    # Batch fetch employees with only needed fields
    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active", "department": ["in", allowed_departments]},
        fields=employee_fields,
    )
    
    if not employees:
        return
    
    employee_names = [e.name for e in employees]
    employee_user_ids = [e.user_id for e in employees if e.user_id]
    
    # Batch fetch all User Meta records
    user_metas = frappe.get_all(
        "User Meta",
        filters={"user": ["in", employee_user_ids]},
        fields=["user", "custom_slack_userid"],
    )
    user_meta_map = {um.user: um.custom_slack_userid for um in user_metas if um.custom_slack_userid}
    
    # Batch fetch all half-day leaves for the date
    half_day_leaves = frappe.get_all(
        "Leave Application",
        filters={
            "employee": ["in", employee_names],
            "half_day_date": str(date),
            "half_day": 1,
            "status": ["in", ["Open", "Approved"]],
        },
        fields=["employee"],
    )
    # Count half-day leaves per employee
    half_day_counts = {}
    for la in half_day_leaves:
        half_day_counts[la.employee] = half_day_counts.get(la.employee, 0) + 1
    
    # Batch fetch all timesheets for the date
    timesheets = frappe.get_all(
        "Timesheet",
        filters={
            "employee": ["in", employee_names],
            "start_date": date,
            "end_date": date,
        },
        fields=["employee", "total_hours"],
    )
    employee_hours = {}
    for ts in timesheets:
        employee_hours[ts.employee] = employee_hours.get(ts.employee, 0) + ts.total_hours
    
    # Batch fetch holidays for the date
    # Get holiday lists for all employees (with fallback to company holiday list)
    employee_holiday_lists = {}
    for employee in employees:
        holiday_list = get_holiday_list_for_employee(employee.name)
        if holiday_list:
            employee_holiday_lists[employee.name] = holiday_list
    
    # Get unique holiday lists and fetch holidays
    unique_holiday_lists = set(employee_holiday_lists.values())
    holidays_on_date = frappe.get_all(
        "Holiday",
        filters={
            "parent": ["in", list(unique_holiday_lists)],
            "holiday_date": date,
        },
        fields=["parent"],
    )
    holiday_lists_with_holiday = {h.parent for h in holidays_on_date}
    
    # Batch fetch full-day leaves for the date
    full_day_leaves = frappe.get_all(
        "Leave Application",
        filters={
            "employee": ["in", employee_names],
            "from_date": ["<=", date],
            "to_date": [">=", date],
            "half_day": 0,
            "status": ["in", ["Open", "Approved"]],
        },
        fields=["employee"],
    )
    employees_on_full_leave = {la.employee for la in full_day_leaves}

    for employee in employees:
        # Check if employee has slack ID
        slack_user_id = user_meta_map.get(employee.user_id)
        if not slack_user_id:
            continue
        
        # Check if holiday or full-day leave
        employee_holiday_list = employee_holiday_lists.get(employee.name)
        if employee_holiday_list and employee_holiday_list in holiday_lists_with_holiday:
            continue
        if employee.name in employees_on_full_leave:
            continue
        
        # Calculate daily norm
        daily_norm = get_daily_norm_from_employee(employee, standard_working_hours)
        
        # Check half day and adjust daily norm
        half_day_count = half_day_counts.get(employee.name, 0)
        # if half day is taken for both the first and second half of the day,
        # then consider full day leave
        if half_day_count > 1:
            continue
        elif half_day_count == 1:
            daily_norm = daily_norm / 2
        
        # Get reported hours
        hour = employee_hours.get(employee.name, 0)
        if hour >= daily_norm:
            continue
        
        # Send slack notification to user
        try:
            args = {
                "date": standard_date_fmt(date),
                "name": employee.employee_name,
                "logged_time": hour,
                "mention": f"<@{slack_user_id}>",
                "daily_norm": daily_norm,
            }
            message = frappe.render_template(reminder_template.response_html, args)
            slack.slack_app.client.chat_postMessage(
                channel=slack_user_id,
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


def get_daily_norm_from_employee(employee, standard_working_hours=8):
    """Calculate daily norm from employee record fields"""
    working_hour = None
    working_frequency = None
    
    # Check if next_pms is installed before accessing custom fields
    if is_next_pms_installed():
        working_hour = employee.get("custom_working_hours")
        working_frequency = employee.get("custom_work_schedule")
    
    if working_hour is None:
        working_hour = standard_working_hours
    
    if not working_frequency:
        working_frequency = "Per Day"
    
    if working_frequency != "Per Day":
        return working_hour / 5
    return working_hour

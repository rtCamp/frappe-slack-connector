import frappe
from frappe.utils import datetime

from frappe_slack_connector.db.employee import get_employee_from_user


def get_employee_working_hours(employee: str = None):
    if not employee:
        employee = get_employee_from_user()
    working_hour, working_frequency = frappe.get_value(
        "Employee",
        employee,
        ["custom_working_hours", "custom_work_schedule"],
    )
    if not working_hour:
        working_hour = frappe.db.get_single_value(
            "HR Settings", "standard_working_hours"
        )
    if not working_frequency:
        working_frequency = "Per Day"
    return {"working_hour": working_hour or 8, "working_frequency": working_frequency}


def get_employee_daily_working_norm(employee: str):
    working_details = get_employee_working_hours(employee)
    if working_details.get("working_frequency") != "Per Day":
        return working_details.get("working_hour") / 5
    return working_details.get("working_hour")


def get_reported_time_by_employee(employee: str, date: datetime.date) -> bool:
    if_exists = frappe.db.exists(
        "Timesheet",
        {
            "employee": employee,
            "start_date": date,
        },
    )
    if not if_exists:
        return 0

    timesheets = frappe.get_all(
        "Timesheet",
        filters={
            "employee": employee,
            "start_date": date,
            "end_date": date,
        },
        fields=["total_hours"],
    )
    total_hours = 0
    for timesheet in timesheets:
        total_hours += timesheet.total_hours
    return total_hours

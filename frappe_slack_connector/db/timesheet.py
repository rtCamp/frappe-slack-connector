import frappe
from frappe.utils import datetime, getdate

from frappe_slack_connector.db.employee import get_employee_from_user


def get_employee_working_hours(employee: str = None):
    """
    Get the working hours and frequency for the given employee
    """
    if not employee:
        employee = get_employee_from_user()

    working_hour = None
    working_frequency = None
    if custom_timesheet_exists():
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
    """
    Get the daily working norm for the given employee
    """
    working_details = get_employee_working_hours(employee)
    if working_details.get("working_frequency") != "Per Day":
        return working_details.get("working_hour") / 5
    return working_details.get("working_hour")


def get_reported_time_by_employee(employee: str, date: datetime.date) -> bool:
    """
    Get the total reported time by the employee for the given date
    """
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


def create_timesheet_detail(
    date: str,
    hours: float,
    description: str,
    task: str,
    employee: str,
    parent: str | None = None,
):
    pms_installed = custom_timesheet_exists()

    if parent:
        timesheet = frappe.get_doc("Timesheet", parent)
    else:
        timesheet = frappe.get_doc({"doctype": "Timesheet", "employee": employee})

    project = None
    custom_is_billable = None

    fields = ["project"]
    if pms_installed:
        fields.append("custom_is_billable")

    field_values = frappe.get_value("Task", task, fields)
    if pms_installed:
        project, custom_is_billable = field_values
    else:
        project = field_values

    logs = {
        "task": task,
        "hours": hours,
        "description": description,
        "from_time": getdate(date),
        "to_time": getdate(date),
        "project": project,
    }

    if pms_installed:
        logs.update({"is_billable": custom_is_billable})

    timesheet.update({"parent_project": project})
    timesheet.append(
        "time_logs",
        logs,
    )
    timesheet.save()


def custom_timesheet_exists() -> bool:
    """
    Check if the custom fields for timesheet doctype exists
    These fields are taken from the frappe_pms app if installed
    """
    installed_apps = frappe.get_installed_apps()
    return "frappe_pms" in installed_apps

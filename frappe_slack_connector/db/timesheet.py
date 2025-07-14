from datetime import timedelta

import frappe
from frappe.utils import datetime, get_datetime

from frappe_slack_connector.db.employee import get_employee_from_user


# NOTE: Slack supports a maximum of 100 options in a select menu
def get_user_projects(user: str, limit: int | None = 99) -> list:
    """
    Get the projects for the given user
    """
    projects = frappe.get_list(
        "Project",
        filters={"status": "Open"},
        fields=["name", "project_name"],
        order_by="modified desc",
        limit=limit,
    )

    return projects


def get_user_tasks(
    user: str,
    project: str | None = None,
    limit: int = 99,
) -> list:
    """
    Get the tasks for the given user
    """
    if project:
        return frappe.get_list(
            "Task",
            filters={
                "status": ["not in", ["Completed", "Cancelled"]],
                "project": project,
            },
            fields=["name", "subject"],
            order_by="modified desc",
            limit=limit,
        )

    # Get tasks for these projects
    tasks = frappe.get_list(
        "Task",
        filters=[
            ["status", "not in", ["Completed", "Cancelled"]],
        ],
        fields=["name", "subject"],
        order_by="modified desc",
        limit=limit,
    )

    return tasks


def get_employee_working_hours(employee: str = "") -> dict:
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
        working_hour = frappe.db.get_single_value("HR Settings", "standard_working_hours")
    if not working_frequency:
        working_frequency = "Per Day"

    return {"working_hour": working_hour or 8, "working_frequency": working_frequency}


def get_employee_daily_working_norm(employee: str) -> int:
    """
    Get the daily working norm for the given employee
    """
    working_details = get_employee_working_hours(employee)
    if working_details.get("working_frequency") != "Per Day":
        return working_details.get("working_hour") / 5
    return working_details.get("working_hour")


def get_reported_time_by_employee(employee: str, date: datetime.date) -> int:
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

    timesheet_date = get_datetime(date)
    logs = {
        "task": task,
        "description": description,
        "from_time": timesheet_date,
        "to_time": timesheet_date + timedelta(hours=hours),
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

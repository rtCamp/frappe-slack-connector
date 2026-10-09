import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import today

from frappe_slack_connector.db.employee import get_employees_on_holiday


def custom_fields_exist() -> bool:
    """
    Check if the custom fields for rtCamp exist in the Leave Application doctype
    """
    # Check if the custom fields exist in the Leave Application doctype
    return frappe.get_meta("Leave Application").has_field("custom_first_halfsecond_half")


def get_employees_on_leave() -> list:
    """
    Get all employees on leave today.

    Only employees whose status is ``Active`` are returned. A leave can be
    approved while the employee is still active and the employee may then
    leave the organisation before the leave date, so the Employee status is
    checked at query time rather than trusting the Leave Application alone.

    Employees for whom today is a holiday on their own holiday list are
    not returned either: a multi-day leave spanning a holiday is not a
    day off on that date.
    """
    current_date = today()

    fields = [
        "employee",
        "employee_name",
        "leave_type",
        "from_date",
        "to_date",
        "status",
        "half_day",
        "half_day_date",
    ]

    if custom_fields_exist():
        fields.append("custom_first_halfsecond_half")

    leave_applications = frappe.get_all(
        "Leave Application",
        filters={
            "from_date": ("<=", current_date),
            "to_date": (">=", current_date),
            "status": (
                "in",
                ["Open", "Approved"],
            ),
        },
        fields=fields,
        order_by="to_date asc",
    )

    if not leave_applications:
        return []

    # Filter on the (small) set of employees on leave today instead of
    # loading every active employee in the company into memory.
    active_employees = set(
        frappe.get_all(
            "Employee",
            filters={
                "name": ("in", {la.employee for la in leave_applications}),
                "status": "Active",
            },
            pluck="name",
        )
    )

    # Employees follow different holiday lists (e.g. support staff work on
    # public holidays), so a leave that spans a date is only "leave" for an
    # employee whose own calendar treats that date as a working day.
    on_holiday = get_employees_on_holiday(sorted(active_employees), current_date)
    active_employees -= on_holiday

    return [la for la in leave_applications if la.employee in active_employees]


def approve_leave(leave_id: str) -> None:
    """
    Approve the leave application
    """
    # Logic to approve the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    if custom_fields_exist():
        apply_workflow(leave_request, "Approve")
    else:
        leave_request.status = "Approved"
        leave_request.save()
        leave_request.submit()
    leave_request.add_comment(comment_type="Info", text="approved via Slack")


def reject_leave(leave_id: str) -> None:
    """
    Reject the leave application
    """
    # Logic to reject the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    if custom_fields_exist():
        apply_workflow(leave_request, "Reject")
    else:
        leave_request.status = "Rejected"
        leave_request.save()
        leave_request.submit()
    leave_request.add_comment(comment_type="Info", text="rejected via Slack")

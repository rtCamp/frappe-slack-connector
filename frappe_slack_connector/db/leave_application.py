import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import today


def custom_fields_exist() -> bool:
    """
    Check if the custom fields for rtCamp exist in the Leave Application doctype
    """
    # Check if the custom fields exist in the Leave Application doctype
    return frappe.get_meta("Leave Application").has_field("custom_first_halfsecond_half")


def get_employees_on_leave() -> list:
    """
    Get all employees on leave today
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

    # 1. Fetch only Active employees
    active_employees = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        pluck="name"
    )

    # Performance safeguard: If no active employees exist, skip the main query
    if not active_employees:
        return []

    # 2. Query Leave Application doctype, filtering by the active employees list
    leave_applications = frappe.get_all(
        "Leave Application",
        filters={
            "from_date": ("<=", current_date),
            "to_date": (">=", current_date),
            "status": (
                "in",
                ["Open", "Approved"],
            ),
            "employee": ("in", active_employees), # <-- NEW FILTER ADDED HERE
        },
        fields=fields,
        order_by="to_date asc",
    )

    return leave_applications


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

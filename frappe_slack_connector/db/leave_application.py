import frappe
from frappe.utils import today


def get_employees_on_leave() -> list:
    """
    Get all employees on leave today
    """
    current_date = today()

    # Query Leave Application doctype
    leave_applications = frappe.get_all(
        "Leave Application",
        filters={"from_date": ("<=", current_date), "to_date": (">=", current_date)},
        fields=[
            "employee",
            "employee_name",
            "leave_type",
            "from_date",
            "to_date",
            "status",
            "half_day",
            "half_day_date",
            "custom_first_halfsecond_half",
        ],
    )

    return leave_applications


def approve_leave(leave_id: str) -> str:
    """
    Approve the leave application
    """
    # Logic to approve the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    leave_request.status = "Approved"
    leave_request.save(ignore_permissions=True)
    leave_request.submit()


def reject_leave(leave_id: str) -> str:
    """
    Reject the leave application
    """
    # Logic to reject the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    leave_request.status = "Rejected"
    leave_request.save(ignore_permissions=True)
    leave_request.submit()

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
        ],
    )

    return leave_applications

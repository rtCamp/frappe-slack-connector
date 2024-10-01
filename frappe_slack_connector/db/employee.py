import frappe
from frappe.utils import datetime
from hrms.hr.utils import get_holiday_list_for_employee

from frappe_slack_connector.helpers.error import generate_error_log


def get_employee_company_email(user_email: str = None):
    """
    Get the company email for the given user email
    """
    # If no user is provided, get the current user
    if not user_email:
        user_email = frappe.session.user_email

    try:
        # Find the Employee record for the user
        employee = frappe.get_all(
            "Employee",
            filters={
                "status": "Active",
            },
            or_filters={
                "user_id": user_email,
                "company_email": user_email,
                "personal_email": user_email,
            },
            fields=["name", "company_email"],
            limit=1,
        )

        if employee:
            # If an Employee record is found, return the company_email
            return employee[0].company_email
        else:
            generate_error_log(f"No Employee record found for user {user_email}")
            return None

    except Exception as e:
        generate_error_log(
            title="Error fetching employee company email",
            exception=e,
        )
        return None


@frappe.whitelist()
def get_employee_from_user(user=None):

    user = frappe.session.user
    employee = frappe.db.get_value("Employee", {"user_id": user})

    if not employee:
        frappe.throw(frappe._("Employee not found"))
    return employee


def get_user_from_employee(employee: str):
    return frappe.get_value("Employee", employee, "user_id")


@frappe.whitelist()
def get_employee(filters=None, fieldname=None):
    import json

    if not fieldname:
        fieldname = ["name", "employee_name", "image"]

    if fieldname and isinstance(fieldname, str):
        fieldname = json.loads(fieldname)

    if filters and isinstance(filters, str):
        filters = json.loads(filters)

    return frappe.db.get_value(
        "Employee", filters=filters, fieldname=fieldname, as_dict=True
    )


def check_if_date_is_holiday(date: datetime.date, employee: str) -> bool:
    holiday_list = get_holiday_list_for_employee(employee)
    return frappe.db.exists(
        "Holiday",
        {
            "holiday_date": date,
            "parent": holiday_list,
        },
    )

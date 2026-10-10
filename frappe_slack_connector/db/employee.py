import frappe
from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from frappe.utils import datetime

from frappe_slack_connector.helpers.error import generate_error_log


def get_employee_company_email(user_email: str = ""):
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


def get_employee_from_user(user=None):
    """
    Get the employee doc for the given user
    """
    user = frappe.session.user
    employee = frappe.db.get_value("Employee", {"user_id": user})

    if not employee:
        frappe.throw(frappe._("Employee not found"))
    return employee


def get_user_from_employee(employee: str):
    """
    Get the user for the given employee
    """
    return frappe.get_value("Employee", employee, "user_id")


def get_employee(filters=None, fieldname=None):
    """
    Get the employee doc for the given filters
    """
    import json

    if not fieldname:
        fieldname = ["name", "employee_name", "image"]

    if fieldname and isinstance(fieldname, str):
        fieldname = json.loads(fieldname)

    if filters and isinstance(filters, str):
        filters = json.loads(filters)

    return frappe.db.get_value("Employee", filters=filters, fieldname=fieldname, as_dict=True)


def check_if_date_is_holiday(date: datetime.date, employee: str) -> bool:
    """
    Check if the given date is a non-working day for the given employee
    """
    holiday_list = get_holiday_list_for_employee(employee, raise_exception=False, as_on=date)
    has_holiday = frappe.db.exists(
        "Holiday",
        {
            "holiday_date": date,
            "parent": holiday_list,
        },
    )

    # Check if it's a full-day leave
    is_leave = frappe.db.exists(
        "Leave Application",
        {
            "employee": employee,
            "from_date": ("<=", date),
            "to_date": (">=", date),
            "half_day": 0,  # This ensures only full day leaves are considered
            "status": (
                "in",
                ["Open", "Approved"],
            ),
        },
    )
    return any((has_holiday, is_leave))


def get_default_holiday_list() -> str | None:
    """
    Holiday list of the default company, which the attendance summary
    treats as the company-wide calendar
    """
    # Global Defaults stores its default_company field under the "company" key
    company = frappe.defaults.get_global_default("company")
    if not company:
        return None
    return frappe.get_cached_value("Company", company, "default_holiday_list")


def is_company_holiday(date: str) -> bool:
    """
    Whether the date is a holiday in the default company's holiday list.
    Without a holiday list only weekends count as non-working days
    """
    holiday_list = get_default_holiday_list()
    if not holiday_list:
        return False
    return is_holiday(holiday_list, date)


def get_employees_on_holiday(employees: list[str], date: str) -> set[str]:
    """
    Subset of ``employees`` for whom ``date`` is a holiday on their own
    holiday list (falling back to their company's). The Holiday rows for
    every distinct list are fetched in one query
    """
    if not employees:
        return set()

    holiday_list_of = {}
    for employee in employees:
        holiday_list = get_holiday_list_for_employee(employee, raise_exception=False, as_on=date)
        if holiday_list:
            holiday_list_of[employee] = holiday_list

    if not holiday_list_of:
        return set()

    lists_with_holiday = set(
        frappe.get_all(
            "Holiday",
            filters={
                "parent": ("in", list(set(holiday_list_of.values()))),
                "holiday_date": date,
            },
            pluck="parent",
        )
    )
    return {employee for employee, holiday_list in holiday_list_of.items() if holiday_list in lists_with_holiday}

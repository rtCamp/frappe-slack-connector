import frappe


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
            frappe.log_error(f"No Employee record found for user {user_email}")
            return None

    except Exception as e:
        frappe.log_error(title="Error fetching employee company email", message=str(e))
        return None

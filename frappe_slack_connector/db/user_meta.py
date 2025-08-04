import frappe

from frappe_slack_connector.helpers.error import generate_error_log


def update_user_meta(user_meta_object: dict, user: str | None = None, upsert: bool = True) -> object:
    """
    Update the User Meta document for the given user.
    """
    if user is None:
        user = frappe.session.user
    user_meta = frappe.db.get_value(
        "User Meta",
        {"user": user},
    )

    if user_meta:
        user_meta = frappe.get_doc("User Meta", user_meta)
    elif not upsert:
        return None
    else:
        user_meta = frappe.get_doc(
            {"doctype": "User Meta", "user": user},
        )

    user_meta.update(user_meta_object)
    user_meta.save(ignore_permissions=True)
    frappe.db.commit()  # commit the changes // nosemgrep

    return user_meta


def get_user_meta(*, user_id: str | None = None, employee_id: str | None = None) -> dict | None:
    """
    Get the User Meta document for the given user or employee.
    """
    try:
        if not user_id and not employee_id:
            raise ValueError("Either user_id or employee_id is required")
        if user_id and employee_id:
            raise ValueError("Only one of user_id or employee_id is required")

        user_meta = None
        if user_id:
            user_meta = frappe.get_doc("User Meta", {"user": user_id})
        else:
            employee_email = frappe.get_value("Employee", employee_id, "user_id")
            if employee_email:
                user_meta = frappe.get_doc("User Meta", {"user": employee_email})
        return user_meta
    except Exception as e:
        generate_error_log(
            title="Error getting User Meta",
            message="Please check the user ID or employee ID and try again.",
            exception=e,
        )
        return None


def get_userid_from_slackid(slack_user_id: str) -> str | None:
    """
    Get the Frappe User ID for the given Slack User ID.
    """
    user_meta = frappe.get_doc("User Meta", {"custom_slack_userid": slack_user_id})
    if not user_meta or not user_meta.user:
        return None
    return user_meta.user


def get_employeeid_from_slackid(slack_user_id: str) -> str | None:
    """
    Get the Employee ID for the given Slack User ID.
    """
    try:
        userid = get_userid_from_slackid(slack_user_id)
        if userid is None:
            return None
        # get the employee id from employee doctype
        employee_id = frappe.get_value("Employee", {"user_id": userid}, "name")
        return employee_id
    except Exception as e:
        generate_error_log(
            title="Error getting Employee ID",
            message="Please check the Slack User ID and try again.",
            exception=e,
        )
        return None

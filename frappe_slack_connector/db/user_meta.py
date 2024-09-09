import frappe


def update_user_meta(
    user_meta_object: dict, user: str | None = None, upsert: bool = True
) -> object:
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
    frappe.db.commit()

    return user_meta


def get_user_meta(*, user_id: str = None, employee_id: str = None) -> dict | None:
    """
    Get the User Meta document for the given user or employee.
    """
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

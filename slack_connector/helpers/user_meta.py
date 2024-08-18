import frappe


def update_user_meta(
    user_meta_object: dict, user: str | None = None, upsert: bool = True
) -> object:
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

import frappe


def update_user_meta(user_github_meta_object: dict, user: str | None = None) -> object:
    if user is None:
        user = frappe.session.user
    user_github_meta = frappe.db.get_value(
        "User Meta",
        {"user": user},
    )

    if user_github_meta:
        user_github_meta = frappe.get_doc("User Meta", user_github_meta)
    else:
        user_github_meta = frappe.get_doc(
            {"doctype": "User Meta", "user": user},
        )

    user_github_meta.update(user_github_meta_object)
    user_github_meta.save(ignore_permissions=True)
    frappe.db.commit()

    return user_github_meta

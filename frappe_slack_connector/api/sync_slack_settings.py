import frappe
from frappe import _

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def sync_slack_data():
    """
    Sync the Slack data with the User Meta
    Enqueues the background job to sync
    """
    frappe.msgprint(_("Syncing Slack data..."))
    frappe.enqueue(sync_slack_job, queue="long", notify=True)


def sync_slack_job(notify: bool = False):
    """
    Background job to sync the Slack data with the User Meta
    """
    try:
        slack = SlackIntegration()
        slack_users = slack.get_slack_users()

        # Batch fetch all existing User Meta records
        existing_user_metas = frappe.get_all(
            "User Meta",
            filters={"user": ["in", list(slack_users.keys())]},
            fields=["name", "user"],
        )
        user_meta_map = {um.user: um.name for um in existing_user_metas}

        # Check Users in Slack but not in ERPNext
        users_not_found = []
        for email, slack_details in slack_users.items():
            try:
                if email in user_meta_map:
                    # Update existing User Meta using set_value (faster than get_doc + save)
                    frappe.db.set_value(
                        "User Meta",
                        user_meta_map[email],
                        {
                            "custom_slack_userid": slack_details["id"],
                            "custom_slack_username": slack_details["name"],
                        },
                        update_modified=True,
                    )
                else:
                    # Create new User Meta - check if user exists first
                    if frappe.db.exists("User", email):
                        user_meta = frappe.get_doc(
                            {
                                "doctype": "User Meta",
                                "user": email,
                                "custom_slack_userid": slack_details["id"],
                                "custom_slack_username": slack_details["name"],
                            }
                        )
                        user_meta.insert(ignore_permissions=True)
                    else:
                        users_not_found.append((email, "User not found in ERPNext"))
            except Exception as e:
                users_not_found.append((email, str(e)))

        # Single commit after all updates
        frappe.db.commit()

        if notify:
            frappe.msgprint(_("Slack data synced successfully"), realtime=True, indicator="green")

        # Check and display employees in ERPNext but not in Slack
        employees = frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["user_id", "employee_name"],
        )

        unset_employees = [emp.employee_name for emp in employees if slack_users.get(emp.user_id) is None]

        if users_not_found:
            if notify:
                frappe.msgprint(
                    f"Users not found in ERPNext: {', '.join(user[0] for user in users_not_found)}",
                    title="Warning",
                    indicator="orange",
                    realtime=True,
                )
            generate_error_log(
                title="Users not found in ERPNext",
                message="\n".join(user[1] for user in users_not_found),
            )

        if unset_employees:
            generate_error_log(
                title="Employees not found in Slack",
                message=f"Users not found in Slack: {', '.join(unset_employees)}",
                msgprint=notify,
                realtime=notify,
            )

    except Exception as e:
        generate_error_log(
            title="Error syncing Slack data",
            exception=e,
            msgprint=notify,
            realtime=notify,
        )

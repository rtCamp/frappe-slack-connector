import frappe
from frappe import _

from frappe_slack_connector.db.user_meta import update_user_meta
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

        # Check Users in Slack but not in ERPNext
        users_not_found = []
        for email, slack_details in slack_users.items():
            try:
                update_user_meta(
                    {
                        "custom_slack_userid": slack_details["id"],
                        "custom_slack_username": slack_details["name"],
                    },
                    user=email,
                )
            except Exception as e:
                users_not_found.append((email, str(e)))

        if notify:
            frappe.msgprint(_("Slack data synced successfully"), realtime=True, indicator="green")

        # Check and display employees in ERPNext but not in Slack
        employees = frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["user_id", "employee_name"],
        )

        unset_employees = []
        for employee in employees:
            if slack_users.get(employee.user_id) is None:
                unset_employees.append(employee.employee_name)

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

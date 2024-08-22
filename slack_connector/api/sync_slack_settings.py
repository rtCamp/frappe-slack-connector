import frappe

from slack_connector.db.user_meta import update_user_meta
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def sync_slack_data():
    frappe.msgprint("Syncing Slack data...")
    try:
        slack = SlackIntegration()
        slack_users = slack.get_slack_users()
        for email, slack_details in slack_users.items():
            update_user_meta(
                {
                    "custom_slack_userid": slack_details["id"],
                    "custom_slack_username": slack_details["name"],
                },
                user=email,
                upsert=False,
            )
        frappe.msgprint("Slack data synced successfully")

        # Check and display employees not found in Slack
        employees = frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["user_id", "employee_name"],
        )
        unset_employees = []
        for employee in employees:
            if slack_users.get(employee.user_id) is None:
                unset_employees.append(employee.employee_name)
        frappe.msgprint(
            f"Employees not found in Slack: {', '.join(unset_employees)}",
            title="Warning",
            indicator="orange",
        )

    except Exception as e:
        frappe.log_error(f"Error syncing Slack data: {str(e)}")
        frappe.throw("Error syncing Slack data")

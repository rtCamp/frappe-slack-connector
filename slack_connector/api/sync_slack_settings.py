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
                    "custom_username": slack_details["id"],
                    "custom_slack_username": slack_details["name"],
                },
                user=email,
                upsert=False,
            )
        frappe.msgprint("Slack data synced successfully")

    except Exception as e:
        frappe.log_error(f"Error syncing Slack data: {str(e)}")
        frappe.throw("Error syncing Slack data")

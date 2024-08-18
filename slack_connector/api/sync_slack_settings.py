import frappe

from slack_connector.helpers.slack_methods import get_slack_users, map_users_to_employee


@frappe.whitelist()
def sync_slack_data():
    frappe.msgprint("Syncing Slack data...")
    try:
        slack_users = get_slack_users()
        map_users_to_employee(slack_users)
        frappe.msgprint("Slack data synced successfully")
    except Exception as e:
        frappe.log_error(f"Error syncing Slack data: {str(e)}")
        frappe.throw("Error syncing Slack data")

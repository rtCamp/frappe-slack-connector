import frappe


@frappe.whitelist()
def sync_slack_data():
    frappe.msgprint("Syncing Slack data...")
    # Get all users

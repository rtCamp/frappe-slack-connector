import frappe
from frappe import _

from slack_connector.helpers.slack_app import slack_app

# from slack_connector.helpers.slack_methods import get_employee_company_email


@frappe.whitelist()
def fetch_user_details(user_email: str) -> None:
    result = slack_app.client.users_lookupByEmail(email=user_email)
    if result["ok"]:
        user_id = result["user"]["id"]
        frappe.msgprint(user_id)
    else:
        frappe.throw(_("User not found in Slack"))

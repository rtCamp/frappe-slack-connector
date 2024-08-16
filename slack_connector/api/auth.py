import frappe

from slack_connector.helpers.slack_app import slack_app
from slack_connector.helpers.slack_methods import get_employee_company_email
from slack_connector.helpers.user_meta import update_user_meta


@frappe.whitelist()
def connect_slack(user_email: str) -> None:
    company_email = get_employee_company_email(user_email)
    slack_user = slack_app.client.users_lookupByEmail(email=company_email)
    slack_id = slack_user["user"]["id"]
    frappe.msgprint(slack_id)
    update_user_meta(
        {
            "custom_username": slack_id,
        },
        user=user_email,
    )

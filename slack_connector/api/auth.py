import frappe

from slack_connector.helpers.slack_app import slack_app
# from slack_connector.helpers.slack_methods import get_employee_company_email
from slack_connector.helpers.user_meta import update_user_meta


@frappe.whitelist()
def connect_slack(user_email: str) -> None:
    company_email = user_email  # get_employee_company_email(user_email)
    slack_user = slack_app.client.users_lookupByEmail(email=company_email)
    slack_id = slack_user["user"]["id"]
    update_user_meta(
        {
            "custom_username": slack_id,
        },
        user=user_email,
    )
    frappe.msgprint(
        msg=f"Slack user {slack_id} connected to {company_email} successfully",
        # "type": "success",
        title="Success",
        indicator="green",
    )

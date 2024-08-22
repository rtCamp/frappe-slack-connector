import frappe

from slack_connector.db.user_meta import update_user_meta
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def connect_slack(user_email: str) -> None:
    slack = SlackIntegration()
    slack_user = slack.get_slack_user(user_email, check_meta=False)
    slack_id = slack_user["id"]
    slack_name = slack_user["name"]
    update_user_meta(
        {
            "custom_slack_userid": slack_id,
            "custom_slack_username": slack_name,
        },
        user=user_email,
    )
    frappe.msgprint(
        msg=f"Slack user {slack_name} connected to {user_email} successfully",
        title="Success",
        indicator="green",
    )

import frappe
from frappe import _

from slack_connector.db.user_meta import update_user_meta
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist(allow_guest=True)
def connect_slack(user_email: str = None) -> None:
    if user_email is None:
        frappe.local.response.http_status_code = 400
        frappe.local.response.message = _("User email is required")
        return

    try:
        slack = SlackIntegration()
        slack_user = slack.get_slack_user(user_email, check_meta=False)
        if not slack_user:
            frappe.local.response.http_status_code = 404
            frappe.local.response.message = _(
                "Slack user not found for the given email"
            )
            return

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

        frappe.local.response.message = _("Slack user connected successfully")
    except Exception as e:
        frappe.log_error(title="Error connecting Slack user", message=str(e))
        frappe.local.response.http_status_code = 500
        frappe.local.response.message = _(
            "An error occurred while connecting Slack user"
        )

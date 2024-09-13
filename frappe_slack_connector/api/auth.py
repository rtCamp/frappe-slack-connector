import frappe
from frappe import _

from frappe_slack_connector.db.user_meta import update_user_meta
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def connect_slack(user_email: str = None) -> None:
    """
    Connect the Slack user to the given user email
    """
    if user_email is None:
        return send_http_response(_("User email is required"), status_code=400)

    try:
        slack = SlackIntegration()
        slack_user = slack.get_slack_user(user_email, check_meta=False)
        if not slack_user:
            frappe.msgprint(
                msg=_("Slack user not found for the given email"),
                title=_("Error"),
                indicator="red",
            )
            return send_http_response(
                _("Slack user not found for the given email"),
                status_code=400,
            )

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

        return send_http_response(_("Slack user connected successfully"))
    except Exception as e:
        generate_error_log(
            title="Error connecting Slack user",
            exception=e,
        )
        return send_http_response(
            _("An error occurred while connecting Slack user"), status_code=500
        )

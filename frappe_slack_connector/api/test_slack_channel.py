import frappe
from frappe import _

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def test_channel(channel_id: str = ""):
    """
    Test the connection to the Slack channel
    Sends a test message to the given channel ID
    """
    if channel_id is None:
        return send_http_response(_("Channel ID is required"), status_code=400)

    slack = SlackIntegration()
    try:
        slack.slack_app.client.chat_postMessage(
            channel=channel_id,
            text=("*This is a test message from ERPNext.*\n" "_You will see list of people on leave daily_\n"),
        )
    except Exception as e:
        send_http_response(
            _("An error occurred while testing channel"),
            status_code=500,
        )
        generate_error_log(
            title=_("Error posting message to Slack"),
            message=_("Please check the channel ID and try again."),
            exception=e,
            msgprint=True,
        )

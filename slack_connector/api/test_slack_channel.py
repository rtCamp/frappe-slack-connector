import frappe
from frappe import _

from slack_connector.helpers.error import generate_error_log
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def test_channel(channel_id: str = None):
    """
    Test the connection to the Slack channel
    Sends a test message to the given channel ID
    """
    if channel_id is None:
        frappe.response.http_status_code = 400
        frappe.response.message = _("Channel ID is required")
        return

    slack = SlackIntegration()
    try:
        slack.slack_app.client.chat_postMessage(
            channel=channel_id,
            text=(
                "*This is a test message from ERPNext.*\n"
                "_You will see list of people on leave daily_\n"
            ),
        )
    except Exception as e:
        frappe.response.http_status_code = 500
        frappe.response.message = _(
            "An error occurred while connecting testing channel"
        )
        generate_error_log(
            title=_("Error posting message to Slack"),
            message=_("Please check the channel ID and try again."),
            exception=e,
            msgprint=True,
        )

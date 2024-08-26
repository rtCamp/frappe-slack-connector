import frappe
from frappe import _

from slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def test_channel(channel_id):
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
        frappe.log_error(title="Error posting message to Slack", message=str(e))
        frappe.throw(
            title=_("Error posting message to Slack"),
            msg=_("Please check the channel ID and try again."),
        )

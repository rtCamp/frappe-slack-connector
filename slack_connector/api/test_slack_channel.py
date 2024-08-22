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
                "_This is a test message from ERPNext. "
                "You will see list of people on leave daily_\n"
                "_Example:_\n"
                "*People on Leave Today*\n"
                "_Employee1_ is on leave from _20-08-2024_ to _22-08-2024_ (Approved)\n"
                "_Employee2_ is on leave from _25-08-2024_ to _27-08-2024_ (Open)"
            ),
        )
    except Exception as e:
        frappe.log_error(title="Error posting message to Slack", message=str(e))
        frappe.throw(
            title=_("Error posting message to Slack"),
            msg=_("Please check the channel ID and try again."),
        )

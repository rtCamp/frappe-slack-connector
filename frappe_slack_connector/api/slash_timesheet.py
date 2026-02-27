import frappe

from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.slack.interactions.timesheet_modal import show_timesheet_modal


@frappe.whitelist(allow_guest=True)  # nosemgrep
def slash_timesheet():
    """
    API endpoint for the Slash command to open the modal for timesheet creation
    Slash command: /timesheet
    """
    slack = SlackIntegration()
    slack_userid = frappe.form_dict.get("user_id")
    slack_trigger_id = frappe.form_dict.get("trigger_id")

    try:
        slack.verify_slack_request(
            signature=frappe.request.headers.get("X-Slack-Signature"),
            timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
            req_data=frappe.request.get_data(as_text=True),
        )
    except Exception:
        return send_http_response("Invalid request", status_code=403)

    show_timesheet_modal(slack, slack_userid, slack_trigger_id)

    return send_http_response(
        status_code=204,
        is_empty=True,
    )

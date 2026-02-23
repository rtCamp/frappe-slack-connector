import json

import frappe
from frappe import _

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.slack.interactions.approve_leave import handler as approve_leave_handler
from frappe_slack_connector.slack.interactions.submit_leave import half_day_checkbox_handler
from frappe_slack_connector.slack.interactions.submit_leave import handler as submit_leave_handler
from frappe_slack_connector.slack.interactions.submit_timesheet import handler as submit_timesheet_handler
from frappe_slack_connector.slack.interactions.timesheet_filters import handle_timesheet_filter
from frappe_slack_connector.slack.interactions.timesheet_modal import show_timesheet_modal


@frappe.whitelist(allow_guest=True)  # nosemgrep: we are verifying request signature
def event():
    """
    Handle the Slack interactions
    This endpoint is called by the Slack API when an interaction occurs, like a button click
    Need to route the interaction to the appropriate handler
    """
    slack = SlackIntegration()

    try:
        try:
            slack.verify_slack_request(
                signature=frappe.request.headers.get("X-Slack-Signature"),
                timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
                req_data=frappe.request.get_data(as_text=True),
            )
        except Exception as e:
            generate_error_log(
                title="Error verifying Slack request",
                exception=e,
            )
            return send_http_response(
                message="Invalid request",
                status_code=403,
            )

        payload = frappe.request.form.get("payload")
        if not payload:
            generate_error_log(
                title="Error processing Slack Interaction",
                message="Payload not found",
            )
            return send_http_response(
                message="Payload not found",
                status_code=400,
            )

        payload = json.loads(payload)
        event_type = payload.get("type")

        if event_type == "block_actions":
            block_id = payload["actions"][0]["block_id"]
            action_id = payload["actions"][0]["action_id"]

            # Ignore the action if starts with ignore
            # Start the action_id in the block to "ignore" if the
            # action payload is not required
            if action_id.startswith("ignore"):
                return
            elif block_id == "daily_reminder_button":
                return show_timesheet_modal(slack, payload["user"]["id"], payload["trigger_id"])
            elif block_id == "half_day_checkbox":
                return half_day_checkbox_handler(slack, payload)
            elif block_id in ("project_block", "task_block"):
                return handle_timesheet_filter(slack, payload)
            else:
                return approve_leave_handler(slack, payload)

        elif event_type == "view_submission":
            if payload["view"]["callback_id"] == "timesheet_modal":
                return submit_timesheet_handler(slack, payload)

            return submit_leave_handler(slack, payload)

        else:
            generate_error_log(
                title="Unknown event type",
                message=event_type,
            )
            return send_http_response(
                message="Unknown event type",
                status_code=400,
            )

    except Exception as e:
        generate_error_log("Error handling the event", exception=e)
        frappe.throw(_("An error occurred while handling the event"), frappe.PermissionError)

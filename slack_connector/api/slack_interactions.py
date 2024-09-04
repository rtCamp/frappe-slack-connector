import frappe
from frappe import _

from slack_connector.db.leave_application import approve_leave, reject_leave
from slack_connector.helpers.error import generate_error_log
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist(allow_guest=True)
def event():
    slack = SlackIntegration()

    try:
        payload = slack.verify_slack_request(
            signature=frappe.request.headers.get("X-Slack-Signature"),
            timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
            req_data=frappe.request.get_data(as_text=True),
            payload=frappe.request.form.get("payload"),
            throw_exc=False,
        )

        if not payload:
            generate_error_log(
                title="Error verifying request",
                message="Payload not found",
            )
            return

        # Check the user who sent the request
        user_id = payload.get("user", {}).get("id")
        if not user_id:
            generate_error_log("User ID not found in payload", msgprint=True)

        action_id = payload["actions"][0]["action_id"]
        leave_id = payload["actions"][0]["value"]

        # Process the action based on action_id
        if action_id == "leave_approve":
            approve_leave(leave_id)
        elif action_id == "leave_reject":
            reject_leave(leave_id)
        else:
            frappe.throw(_("Unknown action"))

        blocks = payload["message"]["blocks"]

        status_text = (
            "Approved :white_check_mark:"
            if action_id == "leave_approve"
            else "Rejected :x:"
        )

        # Replace the actions block with a status update
        for i, block in enumerate(blocks):
            if block.get("block_id") == "leave_actions_block":
                blocks[i] = {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Status:* {status_text}",
                    },
                }
            elif block.get("block_id") == "footer_block":
                # delete the footer block
                blocks.pop(i)

        # Update the message with the new blocks
        slack.slack_app.client.chat_update(
            channel=payload["channel"]["id"],
            ts=payload["container"]["message_ts"],
            blocks=blocks,
        )

    except Exception as e:
        generate_error_log("Error handling the event", exception=e)
        frappe.throw(
            _("An error occurred while handling the event"), frappe.PermissionError
        )

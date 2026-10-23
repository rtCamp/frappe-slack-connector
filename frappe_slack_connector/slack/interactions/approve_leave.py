import contextlib

import frappe
from frappe import _

from frappe_slack_connector.db.leave_application import approve_leave, reject_leave
from frappe_slack_connector.db.user_meta import get_userid_from_slackid
from frappe_slack_connector.helpers.error import exception_message, generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration


def handler(slack: SlackIntegration, payload: dict):
    """
    Handle the interaction when a leave application is approved or rejected
    Update the message in Slack with the status of the leave application
    Update the leave application in ERP accordingly
    """
    # Messages queued before this point belong to earlier work in the request
    messages_before = len(frappe.get_message_log())
    decided = None
    try:
        # Check the user who sent the request
        user_id = payload.get("user", {}).get("id")
        if not user_id:
            generate_error_log("User ID not found in payload", msgprint=True)
        frappe.set_user(get_userid_from_slackid(user_id))  # nosemgrep

        action_id = payload["actions"][0]["action_id"]
        leave_id = payload["actions"][0]["value"]

        # Process the action based on action_id
        if action_id == "leave_approve":
            approve_leave(leave_id)
        elif action_id == "leave_reject":
            reject_leave(leave_id)
        else:
            frappe.throw(_("Unknown action"))
        # The decision is final once it is written: a Slack failure while
        # updating the message below must not roll it back
        frappe.db.commit()
        decided = "approved" if action_id == "leave_approve" else "rejected"

        blocks = payload["message"]["blocks"]

        status_text = "Approved :white_check_mark:" if action_id == "leave_approve" else "Rejected :x:"

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
        # Undo whatever the action wrote before it failed: a hook raising after
        # the submit (another app's on_update) would otherwise leave the leave
        # approved in the database while the approver is shown an error, since
        # the request still commits once this handler returns
        with contextlib.suppress(Exception):  # a rollback failure must not pre-empt the log and the modal
            frappe.db.rollback()
        # Log first: the error modal below needs a live trigger_id, and when it
        # fails too the only trace of the original error would be lost
        generate_error_log(title="Error taking action on leave request", exception=e)
        # A decision that was committed stands; say so, or the approver clicks again
        header = (
            f":warning: Leave {decided}, but the Slack message could not be updated"
            if decided
            else ":warning: Error taking action on leave request"
        )
        # show an error modal with the exception message
        slack.slack_app.client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Error"},
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": header,
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Error Details:*\n```{exception_message(e, since=messages_before)}```",
                        },
                    },
                ],
            },
        )

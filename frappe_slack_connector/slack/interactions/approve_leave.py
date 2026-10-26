import frappe
from frappe import _
from frappe.model.workflow import WorkflowTransitionError

from frappe_slack_connector.db.leave_application import approve_leave, effective_leave_status, reject_leave
from frappe_slack_connector.db.user_meta import get_userid_from_slackid
from frappe_slack_connector.helpers.error import exception_message, generate_error_log
from frappe_slack_connector.override.leave_application import (
    _decided_by,
    _format_decider,
    decision_blocks,
    is_leave_decided,
    leave_message_payload,
)
from frappe_slack_connector.slack.app import SlackIntegration

LEAVE_DECISION_FIELDS = ["status", "docstatus", "modified_by", "modified", "leave_approver"]
# What a decision landing between the handler's read and its action raises: the
# save finds the row changed (TimestampMismatchError); on MariaDB with snapshot
# isolation the locking read in that check fails instead, which Frappe reports
# as a deadlock; the workflow's own state check is kept for completeness
RACE_ERRORS = (WorkflowTransitionError, frappe.TimestampMismatchError, frappe.QueryDeadlockError)


def _current_decision(leave_id: str) -> dict | None:
    """
    The decision the leave already carries (status, who and when), or None
    while it is still open
    """
    row = frappe.db.get_value("Leave Application", leave_id, LEAVE_DECISION_FIELDS, as_dict=True)
    if not row:
        frappe.throw(_("Leave Application {0} not found").format(leave_id))
    if not is_leave_decided(row.status, row.docstatus):
        return None
    status = effective_leave_status(row.status, row.docstatus)
    # modified_by / modified are the last writer and write, which a later edit
    # of a decided leave can move. The workflow records who took a transition
    # and when as a "Workflow" comment whose text is the state reached; prefer
    # the latest one for the current state. A decision taken outside the
    # workflow (a cancel, the scheduled auto-approval) leaves no such comment,
    # and an older one for another state must not be mistaken for it
    decided_by, decided_at = row.modified_by, row.modified
    transitions = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Leave Application",
            "reference_name": leave_id,
            "comment_type": "Workflow",
            "content": status,
        },
        fields=["owner", "creation"],
        order_by="creation desc",
        limit=1,
    )
    if transitions:
        decided_by, decided_at = transitions[0].owner, transitions[0].creation
    return {
        "status": status,
        "decided_by": _decided_by(decided_by, row.leave_approver, status),
        "decided_at": decided_at,
    }


def handler(slack: SlackIntegration, payload: dict):
    """
    Handle the interaction when a leave application is approved or rejected
    Update the message in Slack with the status of the leave application
    Update the leave application in ERP accordingly

    A click on a leave that was decided elsewhere meanwhile (approved from
    Desk, auto-approved by a scheduled job, cancelled by the applicant, or
    by another approver a moment earlier) takes no action: the message is
    rewritten with the decision that was actually taken, by whom and when,
    so the approver sees what happened instead of a workflow error
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
        if action_id not in ("leave_approve", "leave_reject"):
            frappe.throw(_("Unknown action"))

        decision = _current_decision(leave_id)
        if decision is None:
            try:
                if action_id == "leave_approve":
                    approve_leave(leave_id)
                else:
                    reject_leave(leave_id)
            except RACE_ERRORS:
                # Decided between the read above and the action (a race with
                # another approver or a double click). Under REPEATABLE READ
                # the re-read would see the same snapshot as the first read, so
                # end the transaction first, then report the decision that landed
                frappe.db.rollback()
                decision = _current_decision(leave_id)
                if decision is None:
                    raise
                # The swallowed error was raised through msgprint; drop its text
                frappe.clear_messages()
            else:
                # The decision is final once it is written: a Slack failure while
                # updating the message below must not roll it back. A stale click
                # writes nothing and so commits nothing
                frappe.db.commit()
                decided = "approved" if action_id == "leave_approve" else "rejected"
        if decision is None:
            status = "Approved" if action_id == "leave_approve" else "Rejected"
            status_by = None
            decided_at = frappe.utils.now_datetime()
        else:
            status = decision["status"]
            status_by = _format_decider(slack, decision["decided_by"])
            decided_at = decision["decided_at"]

        blocks = payload["message"]["blocks"]

        # Replace the actions block (and the footer prompt) with the decision and when it was taken
        blocks = [b for b in blocks if b.get("block_id") != "footer_block"]
        for i, block in enumerate(blocks):
            if block.get("block_id") == "leave_actions_block":
                blocks[i] = decision_blocks(status, status_by, decided_at)[-1]
                break

        # Update the message with the new blocks
        slack.slack_app.client.chat_update(
            channel=payload["channel"]["id"],
            ts=payload["container"]["message_ts"],
            **leave_message_payload(blocks, text=f"Leave request {status.lower()}"),
        )
    except Exception as e:
        # Undo whatever the action wrote before it failed: a hook raising after
        # the submit (another app's on_update) would otherwise leave the leave
        # approved in the database while the approver is shown an error, since
        # the request still commits once this handler returns
        try:
            frappe.db.rollback()
        except Exception as rollback_error:
            # A rollback that fails with the connection alive (a before_rollback
            # observer raising) would leave the partial writes to be committed
            # when this handler returns normally: record it and let the request
            # fail instead, which makes Frappe roll it back
            generate_error_log(title="Error rolling back the Slack leave action", exception=rollback_error)
            raise
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

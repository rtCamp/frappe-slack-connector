import frappe
from frappe.model.document import Document
from frappe.utils import cint, get_url_to_form
from frappe.utils.synchronization import filelock
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.db.leave_application import (
    APPLICANT_CHANNEL_FIELD,
    APPLICANT_MSG_TS_FIELD,
    custom_fields_exist,
    get_applicant_message_ref,
    get_leave_status,
    store_applicant_message_ref,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.helpers.str_utils import escape_slack_text
from frappe_slack_connector.slack.app import SlackIntegration


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted
    """
    # Both jobs wait for the commit: the applicant job writes the DM reference
    # back to this row, which a fast worker could otherwise miss (or DM about
    # a leave whose insert then rolls back)
    frappe.enqueue(
        send_leave_notification_bg,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
    )
    frappe.enqueue(
        send_leave_notification_to_applicant,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
    )


# Leave statuses that count as a decision on the request, and the header the
# applicant's DM is rewritten with when the leave reaches that status
APPLICANT_DECISION_HEADERS = {
    "Approved": ":white_check_mark: Leave Request Approved",
    "Rejected": ":x: Leave Request Rejected",
    "Cancelled": ":no_entry_sign: Leave Request Cancelled",
}

# Slack errors meaning the stored applicant DM can no longer be edited
MISSING_MESSAGE_ERRORS = ("message_not_found", "channel_not_found")

# The submission DM job and the decision job for one leave can run on
# different workers at the same time; both take this lock so only one of
# them ever posts, and the other finds the stored reference
APPLICANT_DM_LOCK_PREFIX = "fsc_applicant_dm"
APPLICANT_DM_LOCK_TIMEOUT = 60


def _applicant_dm_lock(leave_id: str):
    return filelock(f"{APPLICANT_DM_LOCK_PREFIX}_{leave_id}", timeout=APPLICANT_DM_LOCK_TIMEOUT)


def is_leave_decided(status: str | None, docstatus) -> bool:
    """
    Whether the leave has reached a decision worth showing the applicant.

    HRMS only applies an approved leave on submit, so "Approved" counts once
    the document is submitted; a rejection or cancellation is final as soon
    as it is set (some workflows keep rejected leaves as drafts).
    """
    if status in ("Rejected", "Cancelled"):
        return True
    return status == "Approved" and cint(docstatus) == 1


def _reached_decision(doc: Document) -> bool:
    """
    Whether this save is the one that takes the leave to its decision:
    the submit for an approval (status or docstatus changed), the save that
    sets Rejected, or the cancel
    """
    if doc.flags.in_insert or not is_leave_decided(doc.status, doc.docstatus):
        return False
    if cint(doc.docstatus) == 1:
        return doc.has_value_changed("status") or doc.has_value_changed("docstatus")
    return doc.has_value_changed("status")


def _decided_by(modified_by: str | None, leave_approver: str | None, status: str | None = None) -> str | None:
    """
    The user who took the decision: the acting user (Desk, workflow and the
    Slack handler all set it before the hooks run), falling back to the leave
    approver when the change was made by Administrator or a background job.
    A cancellation is not attributed to the approver; it may be anyone's.
    """
    if modified_by and modified_by != "Administrator":
        return modified_by
    if status == "Cancelled":
        return None
    return leave_approver


def on_update_notify_applicant(doc: Document, method: str | None = None):
    """
    Update the applicant's Slack DM when the leave is approved, rejected or
    cancelled, whether the decision was taken in Desk or from Slack.

    Hooked to ``on_update`` (approve/reject go through save/submit) and
    ``on_cancel`` (HRMS sets status to Cancelled in ``before_cancel``, and a
    cancel does not fire ``on_update``). Inserts are skipped: the submission
    DM already renders the decision when a leave is created as decided.
    """
    if not _reached_decision(doc):
        return
    status = doc.status
    # A reject that saves and submits in one request fires this twice on the
    # same document object (status change, then docstatus change); queue the
    # decision once per status so parallel workers cannot both post a DM
    if doc.flags.get("fsc_applicant_decision_queued") == status:
        return
    doc.flags.fsc_applicant_decision_queued = status
    frappe.enqueue(
        send_leave_decision_to_applicant,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
        status=status,
        decided_by=_decided_by(doc.modified_by, doc.leave_approver, status),
    )


def restore_applicant_message_ref(doc: Document, method: str | None = None):
    """
    Keep the DM reference server-owned across every save.

    The fields are hidden and read-only in the form, but Frappe does not
    enforce that on API writes, and ``db_update`` writes every in-memory
    column. A client could point the reference at any bot message (such as
    the attendance summary) and have it overwritten on approval, and a form
    loaded before the background job stored the reference would wipe it with
    NULL. So: never accept client values on insert, and on update always
    replace the in-memory values with the committed ones.
    """
    # ``is_new`` relies on ``__islocal``, which only insert() sets; a document
    # built in memory and never inserted has no name and nothing stored
    if doc.is_new() or not doc.name:
        doc.set(APPLICANT_CHANNEL_FIELD, None)
        doc.set(APPLICANT_MSG_TS_FIELD, None)
        return
    # check_if_latest has already loaded the committed row (for update), so
    # prefer it over a second query
    previous = doc.get_doc_before_save()
    if previous is not None:
        channel, ts = previous.get(APPLICANT_CHANNEL_FIELD), previous.get(APPLICANT_MSG_TS_FIELD)
    else:
        channel, ts = get_applicant_message_ref(doc.name)
    doc.set(APPLICANT_CHANNEL_FIELD, channel or None)
    doc.set(APPLICANT_MSG_TS_FIELD, ts or None)


def send_leave_notification_to_applicant(doc: Document):
    """
    Send a confirmation message to the applicant and remember the message
    so it can be updated in place once a decision is taken.

    A leave that is already decided when the DM is sent (created directly as
    approved, or decided before the job ran) renders the decision right away.
    If the decision job got there first and already posted the applicant a
    DM, nothing is posted: the applicant must only ever get one message.
    """
    try:
        slack = SlackIntegration()
        user_id = slack.get_slack_user_id(employee_id=doc.employee)
        if not user_id:
            generate_error_log(
                title="Applicant Slack ID not found",
                message=f"No Slack user found for employee {doc.employee} (Leave Application {doc.name})",
            )
            return
        status = doc.status if is_leave_decided(doc.status, doc.docstatus) else None
        status_by = _format_decider(slack, _decided_by(doc.modified_by, doc.leave_approver, status)) if status else None
        with _applicant_dm_lock(doc.name):
            channel, ts = get_applicant_message_ref(doc.name)
            if channel and ts:
                return
            response = slack.slack_app.client.chat_postMessage(
                channel=user_id,
                text=_applicant_text(status),
                blocks=_applicant_blocks(doc, user_id, status=status, status_by=status_by),
            )
            store_applicant_message_ref(doc.name, channel=response.get("channel"), ts=response.get("ts"))
    except Exception as e:
        generate_error_log(
            title="Error posting leave confirmation to applicant",
            exception=e,
        )


def send_leave_decision_to_applicant(doc: Document, status: str, decided_by: str | None = None):
    """
    Rewrite the applicant's "Leave Request Submitted" DM with the decision.

    Falls back to posting a fresh DM when no message reference is stored
    (leaves created before the reference was tracked, or whose first DM
    failed to post) or when Slack no longer knows the stored message.

    The job renders the status it was queued with. When the leave has moved
    on since (approved, then cancelled before this job ran) it does nothing:
    the job queued by the later change renders the current status, and a
    slow older job must not overwrite it.
    """
    try:
        slack = SlackIntegration()
        user_id = slack.get_slack_user_id(employee_id=doc.employee)
        if not user_id:
            generate_error_log(
                title="Applicant Slack ID not found",
                message=f"No Slack user found for employee {doc.employee} (Leave Application {doc.name})",
            )
            return
        text = _applicant_text(status)
        blocks = _applicant_blocks(
            doc,
            user_id,
            status=status,
            status_by=_format_decider(slack, decided_by),
        )
        with _applicant_dm_lock(doc.name):
            if get_leave_status(doc.name) != status:
                return
            # Read the reference from the database rather than from ``doc``: the
            # job receives a snapshot taken before the submission DM was stored
            channel, ts = get_applicant_message_ref(doc.name)
            if channel and ts and _is_applicant_dm(doc, channel, ts):
                try:
                    slack.slack_app.client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
                    return
                except SlackApiError as e:
                    if e.response.get("error") not in MISSING_MESSAGE_ERRORS:
                        raise
                    generate_error_log(
                        title="Applicant leave DM no longer exists, posting a fresh one",
                        message=f"Leave Application {doc.name}: {e.response.get('error')} for {channel}/{ts}",
                    )
            response = slack.slack_app.client.chat_postMessage(channel=user_id, text=text, blocks=blocks)
            store_applicant_message_ref(doc.name, channel=response.get("channel"), ts=response.get("ts"))
    except Exception as e:
        generate_error_log(
            title="Error updating leave decision for applicant",
            exception=e,
        )


def _is_applicant_dm(doc: Document, channel: str, ts: str) -> bool:
    """
    Only ever edit a message in a direct-message channel, and never the
    attendance summary. The reference is server-written, but refuse anyway
    so a bad value can only cost a fresh DM, never another message.
    """
    if not str(channel).startswith("D"):
        generate_error_log(
            title="Applicant leave DM reference is not a DM channel",
            message=f"Leave Application {doc.name}: refusing to update {channel}/{ts}; posting a fresh DM instead",
        )
        return False
    if ts == frappe.db.get_single_value("Slack Settings", "last_attendance_msg_ts"):
        generate_error_log(
            title="Applicant leave DM reference points at the attendance summary",
            message=f"Leave Application {doc.name}: refusing to update {channel}/{ts}; posting a fresh DM instead",
        )
        return False
    return True


def _format_decider(slack: SlackIntegration, user_email: str | None) -> str | None:
    """
    Render the user who took the decision: a Slack mention when mentions are
    enabled and the user has a Slack ID, otherwise their full name
    """
    if not user_email:
        return None
    try:
        slack_id = slack.get_slack_user_id(user_email=user_email)
    except Exception as e:
        generate_error_log(
            title="Error fetching approver slack id",
            exception=e,
        )
        slack_id = None
    if slack_id and frappe.db.get_single_value("Slack Settings", "mention_user"):
        return f"<@{slack_id}>"
    return escape_slack_text(frappe.db.get_value("User", user_email, "full_name") or user_email)


def _applicant_text(status: str | None) -> str:
    """
    Plain-text fallback for the applicant DM, shown in notification previews
    """
    if status:
        return f"Your leave request has been {status.lower()}"
    return "Leave request submitted"


def _applicant_blocks(doc: Document, user_slack: str, *, status: str | None = None, status_by: str | None = None):
    """
    Build the applicant DM blocks for the given leave, optionally with a decision
    """
    return format_leave_submission_blocks(
        leave_id=doc.name,
        employee_name=doc.employee,
        leave_link=get_url_to_form("Leave Application", doc.name),
        leave_type=doc.leave_type,
        leave_submission_date=doc.creation,
        from_date=doc.from_date,
        user_slack=user_slack,
        to_date=doc.to_date,
        reason=doc.description,
        status=status,
        status_by=status_by,
    )


def format_leave_submission_blocks(
    *,
    leave_id: str,
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    user_slack: str,
    from_date: str,
    to_date: str,
    reason: str,
    leave_link: str = "#",
    status: str | None = None,
    status_by: str | None = None,
) -> list:
    """
    Format the blocks for the leave application message.

    With ``status`` (Approved, Rejected or Cancelled) the header reflects the
    decision and a status line, naming ``status_by`` when given, is appended.
    """
    if status:
        header = APPLICANT_DECISION_HEADERS[status]
        intro = f"Hello<@{user_slack}>! Your leave request has been {status.lower()}."
    else:
        header = ":memo: Leave Request Submitted"
        intro = f"Hello<@{user_slack}>! Your leave request has been successfully submitted."
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": intro,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Leave ID:* <{leave_link}|{leave_id}> • *Submitted On:* {standard_date_fmt(leave_submission_date)}",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*From:*\n:calendar: {standard_date_fmt(from_date)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Leave Type:*\n:rocket: {leave_type}",
                },
            ],
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*To:*\n:calendar: {standard_date_fmt(to_date)}",
                },
                {"type": "mrkdwn", "text": f"*Reason:*\n>{reason}"},
            ],
        },
    ]
    if status:
        status_text = f"{status} by {status_by}" if status_by else status
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Status:* {status_text}"},
                },
            ]
        )
    return blocks


def send_leave_notification_bg(doc: Document):
    """
    Send a slack message to the leave approver when
    a new leave application is submitted

    Also send a notification to the attendance channel thread if
    the leave date is today and attendance notification is already sent
    """
    slack = SlackIntegration()
    try:
        approver_slack = slack.get_slack_user_id(user_email=doc.leave_approver)
    except Exception as e:
        generate_error_log(
            title="Error fetching approver slack id",
            exception=e,
        )
        approver_slack = None

    try:
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention_users = frappe.db.get_single_value("Slack Settings", "mention_user")
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name
        day_period = "Full Day"
        if doc.half_day and doc.half_day_date == frappe.utils.today():
            day_period = doc.custom_first_halfsecond_half if custom_fields_exist() else "Half Day"

        # if leave date is today and attendance notification is already sent,
        # send notification to attendance channel thread
        slack_settings = frappe.get_single("Slack Settings")
        if (
            doc.from_date == frappe.utils.today()
            and slack_settings.send_attendance_updates == 1
            and slack_settings.last_attendance_date is not None
            and slack_settings.last_attendance_msg_ts is not None
            and slack_settings.last_attendance_date == frappe.utils.nowdate()
        ):
            slack.slack_app.client.chat_postMessage(
                channel=slack.SLACK_CHANNEL_ID,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{mention if mention_users else doc.employee_name} requested for leave today. "
                            + f"_({day_period})_",
                        },
                    },
                ],
                thread_ts=slack_settings.last_attendance_msg_ts,
                reply_broadcast=True,
            )

        # Send message to approver
        if approver_slack is not None:
            slack.slack_app.client.chat_postMessage(
                channel=approver_slack,
                blocks=format_leave_application_blocks(
                    leave_id=doc.name,
                    leave_link=get_url_to_form("Leave Application", doc.name),
                    employee_name=mention,
                    leave_type=doc.leave_type,
                    is_half_day=doc.half_day,
                    leave_submission_date=standard_date_fmt(doc.creation),
                    from_date=standard_date_fmt(doc.from_date),
                    to_date=standard_date_fmt(doc.to_date),
                    reason=doc.description,
                ),
            )

    except Exception as e:
        generate_error_log(
            title="Error posting message to Slack",
            exception=e,
        )


def format_leave_application_blocks(
    *,
    leave_id: str,
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    from_date: str,
    to_date: str,
    is_half_day: bool,
    reason: str = "",
    employee_link: str = "#",
    leave_link: str = "#",
) -> list:
    """
    Format the blocks for the leave application message
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":memo: New Leave Application",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{employee_name} has submitted a new leave request.",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Leave ID:* <{leave_link}|{leave_id}> ",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Leave Type:*\n:rocket: {leave_type}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Submitted On:*\n:clock3: {leave_submission_date}",
                },
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n:date: {from_date}"},
                {"type": "mrkdwn", "text": f"*To:*\n:date: {to_date}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reason:*\n>{reason if reason else 'No reason provided'}",
            },
        },
    ]

    # Add a context menu indicating it is a half day
    if is_half_day:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Half Day: :white_check_mark:",
                    }
                ],
            }
        )

    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": "leave_actions_block",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "emoji": True, "text": "Approve"},
                        "style": "primary",
                        "value": leave_id,
                        "action_id": "leave_approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "emoji": True, "text": "Reject"},
                        "style": "danger",
                        "value": leave_id,
                        "action_id": "leave_reject",
                    },
                ],
            },
            {
                "type": "context",
                "block_id": "footer_block",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Please review and take action on this leave request.",
                    }
                ],
            },
        ]
    )
    return blocks

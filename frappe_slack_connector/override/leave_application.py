from datetime import UTC, datetime

import frappe
from frappe.model.document import Document
from frappe.utils import cint, convert_utc_to_system_timezone, flt, get_url_to_form, getdate, today
from frappe.utils.synchronization import filelock
from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.db.employee import get_employees_on_holiday
from frappe_slack_connector.db.leave_application import (
    APPLICANT_CHANNEL_FIELD,
    APPLICANT_MSG_TS_FIELD,
    custom_fields_exist,
    effective_leave_status,
    get_applicant_message_ref,
    get_leave_status,
    store_applicant_message_ref,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.helpers.str_utils import escape_slack_text
from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.tasks.attendance_summary import get_leave_type

# Custom fields on Leave Application (fixtures/custom_field.json) holding the
# ts of the reply posted in the attendance summary thread for a same-day leave
# and the ID of the channel it was posted in. The ID comes from Slack's
# response: chat.postMessage accepts a channel name, but chat.update (used to
# strike the reply through) only accepts an ID
ATTENDANCE_REPLY_TS_FIELD = "custom_slack_attendance_reply_ts"
ATTENDANCE_REPLY_CHANNEL_FIELD = "custom_slack_attendance_reply_channel"
# The reply text as posted, so the strike-through wraps exactly what the
# channel saw even if the mention setting or the leave changed since
ATTENDANCE_REPLY_TEXT_FIELD = "custom_slack_attendance_reply_text"
ATTENDANCE_REPLY_FIELDS = (ATTENDANCE_REPLY_TS_FIELD, ATTENDANCE_REPLY_CHANNEL_FIELD, ATTENDANCE_REPLY_TEXT_FIELD)

# Statuses under which a leave covering today is announced (and listed in
# the summary)
ANNOUNCED_STATUSES = ("Open", "Approved")

# The post job and the withdrawal job for one leave run under this lock,
# so a withdrawal cannot slip between the post's checks and its store
ATTENDANCE_REPLY_LOCK_PREFIX = "fsc_attendance_reply"
ATTENDANCE_REPLY_LOCK_TIMEOUT = 60


def _attendance_reply_lock(leave_name: str):
    return filelock(f"{ATTENDANCE_REPLY_LOCK_PREFIX}_{leave_name}", timeout=ATTENDANCE_REPLY_LOCK_TIMEOUT)


def _current_leave_state(leave_name: str):
    """
    The committed row's status, docstatus and stored reply reference, or
    None when the row is gone (deleted before the job ran)
    """
    return frappe.db.get_value(
        "Leave Application",
        leave_name,
        ["status", "docstatus", *ATTENDANCE_REPLY_FIELDS],
        as_dict=True,
        ignore=True,
    )


def _is_announced(state) -> bool:
    """Whether the committed row still belongs in the summary and the thread"""
    return bool(state) and state.status in ANNOUNCED_STATUSES and cint(state.docstatus) != 2


def _covers(leave, day) -> bool:
    """Whether the leave's dates include ``day`` (dates may be strings or dates)"""
    return getdate(leave.from_date) <= day <= getdate(leave.to_date)


def _load_leave(leave_name: str):
    """
    The committed row with everything the thread reply needs (status,
    dates, employee, half-day fields, stored reply), or None when it is gone
    """
    fields = [
        "name",
        "status",
        "docstatus",
        "from_date",
        "to_date",
        "employee",
        "employee_name",
        "half_day",
        "half_day_date",
        *ATTENDANCE_REPLY_FIELDS,
    ]
    if custom_fields_exist():
        fields.append("custom_first_halfsecond_half")
    return frappe.db.get_value("Leave Application", leave_name, fields, as_dict=True, ignore=True)


# Appended to a struck-through thread reply. One word for every path
# (rejected, cancelled, discarded, deleted): the channel only needs to know
# the leave is off, not who called it off
WITHDRAWN_LABEL = "Cancelled"
from frappe_slack_connector.tasks.attendance_summary import update_attendance_summary

# Statuses counted in the attendance summary, and those that take a leave out of it
SUMMARY_INCLUDED_STATUSES = ("Open", "Approved")
SUMMARY_EXCLUDED_STATUSES = ("Rejected", "Cancelled")


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted

    Queued after commit: the approver DM's balance lookup authorises the
    user through the Leave Application row, which a worker started before
    the insert commits cannot see
    """
    # Rows created by Data Import are not announcements. Decide here: the
    # background job runs with fresh frappe.local, where the flag is reset.
    # Both jobs wait for the commit: the thread reply and the applicant DM
    # write their Slack references back to this row, which a fast worker
    # could otherwise miss (or announce a leave whose insert then rolls back)
    frappe.enqueue(
        send_leave_notification_bg,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
        announce_in_thread=not frappe.flags.in_import,
    )
    frappe.enqueue(
        send_leave_notification_to_applicant,
        queue="short",
        enqueue_after_commit=True,
        doc=doc,
    )
    # A leave applied for after the morning post changes today's count
    enqueue_attendance_summary_refresh(doc)


def on_update_refresh_attendance_summary(doc, method=None):
    """
    Refresh today's attendance summary when a leave covering today is
    rejected or cancelled after the summary has been posted

    Wired to ``on_update`` (Desk and Slack rejection, with or without a
    workflow), ``on_cancel`` (submitted leave cancelled) and ``on_discard``
    (draft leave discarded). Only fires when the leave actually leaves the
    summary, so edits to an already-rejected leave and Rejected -> Cancelled
    transitions do not rebuild it.
    """
    if not _leaves_attendance_summary(doc):
        return
    enqueue_attendance_summary_refresh(doc)


def on_trash_refresh_attendance_summary(doc, method=None):
    """
    Refresh today's attendance summary when a leave that is counted in it
    is deleted (deleting a draft runs neither ``on_update`` nor
    ``on_cancel``). The job runs after commit, when the row is gone
    """
    if cint(doc.docstatus) == 2 or doc.status not in SUMMARY_INCLUDED_STATUSES:
        return
    enqueue_attendance_summary_refresh(doc)


def _leaves_attendance_summary(doc) -> bool:
    """
    Whether this save takes the leave out of the summary: its status moved
    from a counted one to Rejected or Cancelled, or the document itself was
    cancelled. A cancel normally sets Cancelled too (HRMS ``before_cancel``),
    but ``ignore_validate`` skips that and leaves the status Approved; the
    summary query drops such a row on docstatus alone, so the hook must too.
    """
    cancelled = cint(doc.docstatus) == 2 and doc.has_value_changed("docstatus")
    withdrawn = doc.status in SUMMARY_EXCLUDED_STATUSES and doc.has_value_changed("status")
    if not (cancelled or withdrawn):
        return False
    previous_status = doc.get_value_before_save("status")
    # No before-save copy means we cannot tell; refresh rather than go stale
    return previous_status is None or previous_status in SUMMARY_INCLUDED_STATUSES


def should_refresh_attendance_summary(doc) -> bool:
    """
    Whether a change to this leave affects today's posted attendance summary
    True only when attendance updates are enabled, today's summary has
    already been posted and the leave covers today. If the summary has not
    been posted yet it will reflect the current state on its own.
    """
    slack_settings = frappe.get_single("Slack Settings")
    if (
        slack_settings.send_attendance_updates != 1
        or not slack_settings.last_attendance_msg_ts
        or not slack_settings.last_attendance_date
    ):
        return False

    current_date = getdate(today())
    if getdate(slack_settings.last_attendance_date) != current_date:
        return False

    return getdate(doc.from_date) <= current_date <= getdate(doc.to_date)


def enqueue_attendance_summary_refresh(doc) -> None:
    """
    Enqueue the in-place rebuild of today's attendance summary if the
    leave affects it

    The job reads the Leave Application table, so it is queued after the
    current transaction commits; otherwise the worker could rebuild the
    summary from the state before this change.
    """
    if not should_refresh_attendance_summary(doc):
        return
    frappe.enqueue(
        update_attendance_summary,
        queue="short",
        enqueue_after_commit=True,
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
    status = effective_leave_status(status, docstatus)
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
    # Submit and cancel change docstatus; a cancel that skipped the HRMS hook
    # (ignore_validate) changes nothing else, and still is a decision
    if cint(doc.docstatus) in (1, 2):
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
    status = effective_leave_status(doc.status, doc.docstatus)
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
        # Lock the row until this save commits: a job that stored the
        # reference just before waits for the save and its write is read here
        # rather than overwritten by db_update, and a job that wants to store
        # one waits for the save
        channel, ts = get_applicant_message_ref(doc.name, for_update=True)
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
        status = (
            effective_leave_status(doc.status, doc.docstatus) if is_leave_decided(doc.status, doc.docstatus) else None
        )
        status_by = _format_decider(slack, _decided_by(doc.modified_by, doc.leave_approver, status)) if status else None
        with _applicant_dm_lock(doc.name):
            channel, ts = get_applicant_message_ref(doc.name)
            if channel and ts:
                return
            response = slack.slack_app.client.chat_postMessage(
                channel=user_id,
                text=_applicant_text(status),
                blocks=_applicant_blocks(
                    doc, user_id, status=status, status_by=status_by, leave_balance=message_leave_balance(doc)
                ),
            )
            store_applicant_message_ref(doc.name, channel=response.get("channel"), ts=response.get("ts"))
            # Commit before the lock is released: the job's own commit only comes
            # after this function returns, and a worker waiting on the lock would
            # otherwise still read an empty reference and post a second DM
            frappe.db.commit()  # nosemgrep
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
            leave_balance=message_leave_balance(doc),
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
            # Commit before the lock is released: the job's own commit only comes
            # after this function returns, and a worker waiting on the lock would
            # otherwise still read an empty reference and post a second DM
            frappe.db.commit()  # nosemgrep
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


def _applicant_blocks(
    doc: Document,
    user_slack: str,
    *,
    status: str | None = None,
    status_by: str | None = None,
    leave_balance: float | None = None,
):
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
        duration=format_leave_duration(doc),
        total_days=flt(doc.total_leave_days),
        leave_balance=leave_balance,
        reason=doc.description,
        status=status,
        status_by=status_by,
    )


def message_leave_balance(doc: Document) -> float | None:
    """
    Balance before this request for the approver and applicant messages, or
    None for a Leave Without Pay type: it has no allocation, so no balance
    """
    if frappe.db.get_value("Leave Type", doc.leave_type, "is_lwp"):
        return None
    return get_leave_balance(doc)


def leave_detail_blocks(
    *,
    leave_type: str,
    leave_submission_date: str,
    from_date: str,
    to_date: str,
    duration: str,
    total_days: float | None = None,
    leave_balance: float | None = None,
) -> list:
    """
    The leave detail field sections shared by the approver DM and the
    applicant DM: type and submission date, the date range, then the
    duration with the requested days and the balance before the request
    when given. Dates and the duration arrive formatted
    """
    details = [{"type": "mrkdwn", "text": f"*Duration:*\n:hourglass_flowing_sand: {duration}"}]
    if total_days is not None:
        details.append({"type": "mrkdwn", "text": f"*Requested:*\n{flt(total_days, 2):g} day(s)"})
    if leave_balance is not None:
        details.append(
            {
                "type": "mrkdwn",
                "text": f"*Balance before this request:*\n{flt(leave_balance, 2):g} day(s)",
            }
        )
    return [
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
        {"type": "section", "fields": details},
    ]


def reason_block(reason: str | None) -> dict:
    """
    The quoted reason section, shared by the approver and applicant DMs
    """
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Reason:*\n>{reason if reason else 'No reason provided'}",
        },
    }


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
    duration: str,
    leave_link: str = "#",
    total_days: float | None = None,
    leave_balance: float | None = None,
    status: str | None = None,
    status_by: str | None = None,
) -> list:
    """
    Format the blocks for the applicant's leave confirmation message, with
    the same detail fields the approver sees (duration, requested days and
    the balance before the request when given).

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
                    "text": f"*Leave ID:* <{leave_link}|{leave_id}>",
                }
            ],
        },
        {"type": "divider"},
        *leave_detail_blocks(
            leave_type=leave_type,
            leave_submission_date=standard_date_fmt(leave_submission_date),
            from_date=standard_date_fmt(from_date),
            to_date=standard_date_fmt(to_date),
            duration=duration,
            total_days=total_days,
            leave_balance=leave_balance,
        ),
        reason_block(reason),
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


def _same_day_reply_name(slack: SlackIntegration, employee: str, employee_name: str, mention_user) -> str:
    """Mention when the employee has a Slack id and mentions are on, else the escaped name"""
    user_slack = slack.get_slack_user_id(employee_id=employee)
    return f"<@{user_slack}>" if user_slack and mention_user else escape_slack_text(employee_name)


def _same_day_reply_text(name: str, day_period: str) -> str:
    return f"{name} is on leave today. _({day_period})_"


def _same_day_reply_block(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def post_same_day_leave_to_attendance_thread(doc: Document, slack: SlackIntegration | None = None) -> str | None:
    """
    Reply in today's attendance summary thread when the leave covers today
    and the summary has already been posted (a leave applied for before the
    summary is simply included in the summary itself)

    The reply ``ts`` and channel ID are stored on the Leave Application so
    the reply can be struck through if the leave is rejected or cancelled
    later the same day.
    Returns the ``ts`` of the reply, or None when nothing was posted
    """
    # A leave created already decided (e.g. Rejected) is not an announcement
    if doc.status not in ANNOUNCED_STATUSES:
        return None

    today = getdate(frappe.utils.today())
    slack_settings = frappe.get_single("Slack Settings")
    if (
        slack_settings.send_attendance_updates != 1
        or not slack_settings.last_attendance_msg_ts
        or not slack_settings.last_attendance_date
        or getdate(slack_settings.last_attendance_date) != today
    ):
        return None

    # Cheap pre-check on the queued row; everything is checked again below
    # from the committed row
    if not _covers(doc, today):
        return None

    slack = slack or SlackIntegration()
    # A celebrations-only install may have no attendance channel configured
    if not slack.SLACK_CHANNEL_ID:
        return None

    with _attendance_reply_lock(doc.name):
        # ``doc`` is the row as it was inserted and may be stale by now: the
        # leave may have been rejected, cancelled or deleted (the withdrawal
        # hook found nothing stored then), its dates or half day edited, and
        # a stored reply means a duplicate job. Every gate and the text come
        # from the committed row
        current = _load_leave(doc.name)
        if (
            current is None
            or not _is_announced(current)
            or current.get(ATTENDANCE_REPLY_TS_FIELD)
            or not _covers(current, today)
        ):
            return None

        # Match the summary, which only lists active employees
        if frappe.db.get_value("Employee", current.employee, "status") != "Active":
            return None

        # Same rule as the summary: a leave spanning today is only "leave"
        # when today is a working day on the employee's own holiday list
        if current.employee in get_employees_on_holiday([current.employee], today):
            return None

        name = _same_day_reply_name(slack, current.employee, current.employee_name, slack_settings.mention_user)
        day_period = get_leave_type(current, on_date=today)
        reply_text = _same_day_reply_text(name, day_period)

        # The thread lives where the summary was posted. The summary job
        # stores that channel's ID (when it has the field for it); the
        # setting is the fallback, and may have been changed since the morning
        response = slack.slack_app.client.chat_postMessage(
            channel=slack_settings.get("last_attendance_channel_id") or slack.SLACK_CHANNEL_ID,
            blocks=[_same_day_reply_block(reply_text)],
            thread_ts=slack_settings.last_attendance_msg_ts,
            reply_broadcast=True,
        )
        reply_ts = response["ts"]
        channel = response.get("channel")
        frappe.db.set_value(
            "Leave Application",
            doc.name,
            {
                ATTENDANCE_REPLY_TS_FIELD: reply_ts,
                ATTENDANCE_REPLY_CHANNEL_FIELD: channel,
                ATTENDANCE_REPLY_TEXT_FIELD: reply_text,
            },
            update_modified=False,
        )

        # Commit under the lock: the withdrawal job for this leave waits on
        # the lock and must find the reference, and the job's own commit only
        # comes after this function returns (a manual commit, so the lock
        # really serialises check, post and store)
        frappe.db.commit()  # nosemgrep

        # A withdrawal that committed while Slack was posting enqueued a job
        # that ran before this one stored anything, so take the reply back here
        state = _current_leave_state(doc.name)
        if not _is_announced(state):
            job = dict(
                leave_name=doc.name,
                reply_ts=reply_ts,
                channel=channel,
                reply_text=reply_text,
                employee=current.employee,
                employee_name=current.employee_name,
                day_period=day_period,
            )
            if not _strike_reply(slack, **job) and state is None:
                # The row is gone (deleted), so nothing keeps the reference for
                # a later attempt: queue one more withdrawal carrying it
                frappe.enqueue(withdraw_attendance_reply_bg, queue="short", **job)
    return reply_ts


def send_leave_notification_bg(doc: Document, announce_in_thread: bool = True):
    """
    Send a slack message to the leave approver when
    a new leave application is submitted

    Also send a notification to the attendance channel thread if
    the leave covers today and attendance notification is already sent
    (skipped when ``announce_in_thread`` is False, e.g. for imported rows)
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

    if announce_in_thread:
        try:
            post_same_day_leave_to_attendance_thread(doc, slack=slack)
        except Exception as e:
            generate_error_log(
                title="Error posting same-day leave to attendance thread",
                exception=e,
            )

    try:
        user_slack = slack.get_slack_user_id(employee_id=doc.employee)
        mention = f"<@{user_slack}>" if user_slack else doc.employee_name

        # Send message to approver
        if approver_slack is not None:
            slack.slack_app.client.chat_postMessage(
                channel=approver_slack,
                # Fallback for notification previews; the blocks carry the details
                text=f"New leave application from {escape_slack_text(doc.employee_name)}",
                blocks=format_leave_application_blocks(
                    leave_id=doc.name,
                    leave_link=get_url_to_form("Leave Application", doc.name),
                    employee_name=mention,
                    leave_type=doc.leave_type,
                    duration=format_leave_duration(doc),
                    leave_submission_date=standard_date_fmt(doc.creation),
                    from_date=standard_date_fmt(doc.from_date),
                    to_date=standard_date_fmt(doc.to_date),
                    reason=doc.description,
                    total_days=flt(doc.total_leave_days),
                    leave_balance=message_leave_balance(doc),
                ),
            )

    except Exception as e:
        generate_error_log(
            title="Error posting message to Slack",
            exception=e,
        )


def _reply_posted_today(reply_ts: str) -> bool:
    """
    Whether a Slack message ``ts`` falls on today's date in the site timezone
    """
    # A Slack ts is a Unix epoch in seconds (with a sequence suffix after the
    # dot), so read it as UTC and shift it into the site timezone before
    # comparing calendar dates
    posted_at = convert_utc_to_system_timezone(datetime.fromtimestamp(float(reply_ts), tz=UTC))
    return getdate(posted_at) == getdate(frappe.utils.today())


def get_attendance_reply_ref(leave_name: str, *, for_update: bool = False) -> tuple[str | None, str | None, str | None]:
    """
    The stored (ts, channel ID, posted text) of the attendance thread reply
    for the leave, read from the database; all None when nothing is stored
    or the row is gone. ``for_update`` locks the row, so a save that started before the
    post job wrote the reference waits for that write to commit and reads
    it, instead of putting its stale empty value back
    """
    # ignore=True: nothing to restore while the custom fields are not yet
    # installed (a save between the code upgrade and the fixture sync)
    stored = frappe.db.get_value(
        "Leave Application",
        leave_name,
        list(ATTENDANCE_REPLY_FIELDS),
        as_dict=True,
        ignore=True,
        **({"for_update": True} if for_update else {}),
    )
    if not stored:
        return None, None, None
    return (
        stored.get(ATTENDANCE_REPLY_TS_FIELD) or None,
        stored.get(ATTENDANCE_REPLY_CHANNEL_FIELD) or None,
        stored.get(ATTENDANCE_REPLY_TEXT_FIELD) or None,
    )


def restore_attendance_reply_ts(doc: Document, method=None):
    """
    Keep the stored reply ts and channel server-owned

    The fields are hidden and read-only, but Frappe does not enforce
    ``read_only`` on API writes, and the bot edits whatever message is
    stored here. Only the background jobs may write or clear them with
    ``db.set_value``, so the values a client sends are never trusted: a new
    doc gets them blanked, and an existing doc gets the database values put
    back (this also stops a form loaded before the job ran from erasing them
    with ``db_update``, which writes every column)
    """
    # is_new() only knows about docs going through insert(); a doc built in
    # memory and never inserted has no name either
    if doc.is_new() or not doc.name:
        reply_ts, channel, reply_text = None, None, None
    else:
        reply_ts, channel, reply_text = get_attendance_reply_ref(doc.name, for_update=True)

    doc.set(ATTENDANCE_REPLY_TS_FIELD, reply_ts)
    doc.set(ATTENDANCE_REPLY_CHANNEL_FIELD, channel)
    doc.set(ATTENDANCE_REPLY_TEXT_FIELD, reply_text)


def _enqueue_attendance_reply_withdrawal(doc: Document):
    """
    Enqueue marking the attendance thread reply for the leave as withdrawn,
    when the leave covers today. The job decides from the committed row:
    the reply may not be posted yet when the withdrawal is saved (the post
    job for the insert runs after its own commit), so the decision cannot
    be taken here from what is stored now. Never raises: a bad date or a
    queue outage must not block the user's action
    """
    try:
        today = getdate(frappe.utils.today())
        if not (getdate(doc.from_date) <= today <= getdate(doc.to_date)):
            return

        # Never trust the values on the doc (see restore_attendance_reply_ts):
        # read what is stored, as the fallback for a row that is gone by the
        # time the job runs (on_trash)
        reply_ts, channel, reply_text = get_attendance_reply_ref(doc.name)

        # Only edit once the status change is committed: on_update runs
        # before on_submit, which can still throw and roll back
        frappe.enqueue(
            withdraw_attendance_reply_bg,
            queue="short",
            enqueue_after_commit=True,
            leave_name=doc.name,
            reply_ts=reply_ts,
            channel=channel,
            reply_text=reply_text,
            employee=doc.employee,
            employee_name=doc.employee_name,
            day_period=get_leave_type(doc, on_date=today),
        )
    except Exception as e:
        generate_error_log(
            title="Error scheduling attendance thread reply withdrawal",
            exception=e,
        )


def on_update_withdraw_attendance_reply(doc: Document, method=None):
    """
    Strike through the same-day attendance thread reply when the leave is
    rejected or cancelled on the day the reply was posted. The reply stays
    in the thread as history, marked "(Cancelled)" either way

    Wired to ``on_update``, ``on_update_after_submit``, ``on_cancel`` and
    ``on_discard``: Frappe only runs ``on_cancel`` for a cancel, HRMS sets
    status to Cancelled in ``before_cancel`` and ``on_discard``, and a
    workflow may move a submitted leave to Rejected. A cancel that skips
    ``before_cancel`` (``ignore_validate``) keeps the status Approved, so a
    document cancelled outright counts too: the summary drops it on
    docstatus alone and the reply must follow
    """
    withdrawn = doc.has_value_changed("status") and doc.status in ("Rejected", "Cancelled")
    cancelled = cint(doc.docstatus) == 2 and doc.has_value_changed("docstatus")
    if not (withdrawn or cancelled):
        return

    _enqueue_attendance_reply_withdrawal(doc)


def on_trash_withdraw_attendance_reply(doc: Document, method=None):
    """
    Strike through the same-day attendance thread reply when the leave is
    deleted (deleting a draft runs neither ``on_update`` nor ``on_cancel``)
    """
    _enqueue_attendance_reply_withdrawal(doc)


def withdraw_attendance_reply_bg(
    leave_name: str,
    reply_ts: str | None = None,
    *,
    employee: str,
    employee_name: str,
    day_period: str,
    channel: str | None = None,
    reply_text: str | None = None,
):
    """
    Edit the attendance thread reply ``reply_ts`` posted for the leave
    ``leave_name`` so it reads struck through with "(Cancelled)" appended, and
    clear the stored reference. Nothing is deleted: the thread keeps a record
    of the leave that was announced and then taken back. Editing a broadcast
    reply changes it in the thread and in the channel

    ``channel`` is the ID Slack reported when the reply was posted; the
    attendance channel setting is only a fallback for a reply stored before
    the ID was, and fails when that setting holds a channel name
    """
    with _attendance_reply_lock(leave_name):
        # The committed row wins (the post job may have stored the reply
        # after the withdrawal was saved, and commits under this lock); the
        # values passed in are the fallback for a row that is gone (on_trash)
        state = _current_leave_state(leave_name)
        if state is not None:
            # A later transition put the leave back in the summary (rejected,
            # then reopened and approved): its reply stands
            if _is_announced(state):
                return
            reply_ts = state.get(ATTENDANCE_REPLY_TS_FIELD) or None
            channel = state.get(ATTENDANCE_REPLY_CHANNEL_FIELD) or None
            reply_text = state.get(ATTENDANCE_REPLY_TEXT_FIELD) or None
        # A reply from an earlier day is history and is left alone
        try:
            if not reply_ts or not _reply_posted_today(reply_ts):
                return
        except Exception as e:
            generate_error_log(
                title="Error marking attendance thread reply as withdrawn",
                message=f"Leave Application {leave_name} stores an unreadable reply ts {reply_ts!r}",
                exception=e,
            )
            return

        # The bot must only ever edit its own thread replies, never the
        # summary message the thread hangs off
        if reply_ts == frappe.db.get_single_value("Slack Settings", "last_attendance_msg_ts"):
            generate_error_log(
                title="Refused to edit the attendance summary",
                message=f"Leave Application {leave_name} stores the summary ts {reply_ts} as its reply ts",
            )
            return

        _strike_reply(
            None,
            leave_name=leave_name,
            reply_ts=reply_ts,
            channel=channel,
            reply_text=reply_text,
            employee=employee,
            employee_name=employee_name,
            day_period=day_period,
        )


def _strike_reply(
    slack: SlackIntegration | None,
    *,
    leave_name: str,
    reply_ts: str,
    channel: str | None,
    employee: str,
    employee_name: str,
    day_period: str,
    reply_text: str | None = None,
) -> bool:
    """
    Edit the reply into its struck-through form and clear the stored
    reference; True when the reference was cleared. The reference is
    cleared after a successful edit, or when Slack says the message is gone
    for good; a transient failure (rate limit, timeout, outage) is logged
    and keeps the reference, so a later withdrawal the same day still has a
    handle on the reply

    ``reply_text`` is the text as posted; the struck-through message wraps
    exactly that, so a mention setting or leave edited since does not
    rewrite history. It is rebuilt only for a reply stored before the text
    was kept
    """
    cleared = True
    try:
        slack = slack or SlackIntegration()
        if not reply_text:
            mention_user = frappe.db.get_single_value("Slack Settings", "mention_user")
            reply_text = _same_day_reply_text(
                _same_day_reply_name(slack, employee, employee_name, mention_user), day_period
            )
        text = f"~{reply_text}~ ({WITHDRAWN_LABEL})"
        slack.slack_app.client.chat_update(
            channel=channel or slack.SLACK_CHANNEL_ID,
            ts=reply_ts,
            blocks=[_same_day_reply_block(text)],
            text=f"{escape_slack_text(employee_name)} is on leave today ({WITHDRAWN_LABEL})",
        )
    except SlackApiError as e:
        # The reply may already be gone (deleted by hand): nothing to keep.
        # channel_not_found only proves that when the stored channel ID was
        # used; the setting fallback may be a name, which chat.update rejects
        # with the same error while the reply is still there
        error = e.response.get("error")
        cleared = error == "message_not_found" or (error == "channel_not_found" and bool(channel))
        generate_error_log(
            title="Error marking attendance thread reply as withdrawn",
            message=f"Leave Application {leave_name}: reply {reply_ts} in {channel or 'the attendance channel'}",
            exception=e,
        )
    except Exception as e:
        cleared = False
        generate_error_log(
            title="Error marking attendance thread reply as withdrawn",
            message=f"Leave Application {leave_name}: reply {reply_ts} in {channel or 'the attendance channel'}",
            exception=e,
        )

    if not cleared:
        return False
    # A no-op when the leave itself has been deleted
    frappe.db.set_value(
        "Leave Application",
        leave_name,
        dict.fromkeys(ATTENDANCE_REPLY_FIELDS),
        update_modified=False,
    )
    return True


def get_leave_balance(doc: Document) -> float | None:
    """
    Balance before this request, computed with the same HRMS call the Desk
    form uses

    The ``leave_balance`` stored on the application cannot be trusted as a
    signal: the form fills it, but an application created on the server
    (the Slack modal) has none, and the column is NOT NULL so any reload
    before this job runs (a Notification on save does one) reads it back
    as 0, which would show a zero balance and a false warning. So the
    balance is always computed here; the stored value is only the fallback
    when the lookup fails, and None means nothing to show

    ``leave_application`` lets HRMS grant the lookup to a user who can read
    this application but not the Employee (the job runs as the user who
    saved the leave)
    """
    try:
        balance = flt(
            get_leave_balance_on(
                doc.employee,
                doc.leave_type,
                doc.from_date,
                doc.to_date,
                consider_all_leaves_in_the_allocation_period=True,
                leave_application=doc.name,
            )
        )
    except Exception as e:
        generate_error_log(
            title="Error fetching leave balance",
            exception=e,
        )
        return flt(doc.leave_balance) if doc.leave_balance else None

    # A leave inserted already approved and submitted (API, import) has its
    # ledger entry by the time the job runs, so HRMS has deducted it: add it
    # back to show the balance before this request, like for a draft. A
    # rejected leave is submitted without a ledger entry
    if cint(doc.docstatus) == 1 and doc.status == "Approved":
        balance += flt(doc.total_leave_days)
    return balance


def format_leave_duration(doc: Document) -> str:
    """
    Describe how much of the leave is taken: "Full Day", the half for a
    single-day half-day leave, or the half-day date for a multi-day leave
    that includes one half day
    """
    if not doc.half_day:
        return "Full Day"

    period = doc.custom_first_halfsecond_half if custom_fields_exist() else None
    if getdate(doc.from_date) == getdate(doc.to_date):
        return period or "Half Day"

    half_day_on = f"Half day on {standard_date_fmt(doc.half_day_date)}" if doc.half_day_date else "One half day"
    return f"{half_day_on} — {period}" if period else half_day_on


def format_leave_application_blocks(
    *,
    leave_id: str,
    employee_name: str,
    leave_type: str,
    leave_submission_date: str,
    from_date: str,
    to_date: str,
    duration: str,
    reason: str = "",
    employee_link: str = "#",
    leave_link: str = "#",
    total_days: float | None = None,
    leave_balance: float | None = None,
) -> list:
    """
    Format the blocks for the leave application message

    `leave_balance` is the balance before this request as computed when the
    message is built; pass None to omit it (e.g. for Leave Without Pay)
    """
    requested = flt(total_days, 2) if total_days is not None else None
    balance = flt(leave_balance, 2) if leave_balance is not None else None

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
        *leave_detail_blocks(
            leave_type=leave_type,
            leave_submission_date=leave_submission_date,
            from_date=from_date,
            to_date=to_date,
            duration=duration,
            total_days=total_days,
            leave_balance=leave_balance,
        ),
    ]

    # Warn the approver when the request cannot be covered by the recorded balance
    if requested is not None and balance is not None and requested > balance:
        shortfall = flt(requested - balance, 2)
        resulting_balance = flt(balance - requested, 2)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":warning: *Insufficient balance:* this request exceeds the available balance "
                    + f"by {shortfall:g} day(s). Approving it will take the balance to {resulting_balance:g}.",
                },
            }
        )

    blocks.append(reason_block(reason))

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

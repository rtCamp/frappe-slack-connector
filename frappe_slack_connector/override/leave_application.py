from datetime import UTC, datetime

import frappe
from frappe.model.document import Document
from frappe.utils import cint, convert_utc_to_system_timezone, flt, get_url_to_form, getdate
from frappe.utils.synchronization import filelock
from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.db.employee import get_employees_on_holiday
from frappe_slack_connector.db.leave_application import custom_fields_exist
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
    # Both jobs wait for the commit: the thread reply writes its ts back to
    # this row, which a fast worker could otherwise miss (or announce a leave
    # whose insert then rolls back)
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


def send_leave_notification_to_applicant(doc: Document):
    # Send a confirmation message to the user
    slack = SlackIntegration()
    user_id = slack.get_slack_user_id(employee_id=doc.employee)
    slack.slack_app.client.chat_postMessage(
        channel=user_id,
        blocks=format_leave_submission_blocks(
            leave_id=doc.name,
            employee_name=doc.employee,
            leave_link=get_url_to_form("Leave Application", doc.name),
            leave_type=doc.leave_type,
            leave_submission_date=doc.creation,
            from_date=doc.from_date,
            user_slack=user_id,
            to_date=doc.to_date,
            reason=doc.description,
        ),
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
) -> list:
    """
    Format the blocks for the leave application message
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":memo: Leave Request Submitted",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Hello<@{user_slack}>! Your leave request has been successfully submitted.",
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
            # Leave Without Pay types have no allocation, so the balance is meaningless
            leave_balance = None
            if not frappe.db.get_value("Leave Type", doc.leave_type, "is_lwp"):
                leave_balance = get_leave_balance(doc)

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
                    leave_balance=leave_balance,
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

    details = [{"type": "mrkdwn", "text": f"*Duration:*\n:hourglass_flowing_sand: {duration}"}]
    if requested is not None:
        details.append({"type": "mrkdwn", "text": f"*Requested:*\n{requested:g} day(s)"})
    if balance is not None:
        details.append(
            {
                "type": "mrkdwn",
                "text": f"*Balance before this request:*\n{balance:g} day(s)",
            }
        )

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
        {"type": "section", "fields": details},
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

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reason:*\n>{reason if reason else 'No reason provided'}",
            },
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

import hashlib
import json
from datetime import datetime

import frappe
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from frappe import _
from frappe.utils import get_time, getdate, today
from frappe.utils.synchronization import filelock

from frappe_slack_connector.db.leave_application import (
    custom_fields_exist,
    get_employees_on_leave,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration

# Name of the site-level lock that serialises in-place summary updates
ATTENDANCE_UPDATE_LOCK = "fsc_attendance_summary_update"

# Cache key prefix for the fingerprint of what a summary message currently
# shows, keyed by the message ts; kept for two days so the entry outlives the
# message it describes without piling up
ATTENDANCE_HASH_CACHE_PREFIX = "fsc_attendance_summary_hash"
ATTENDANCE_HASH_TTL_SECONDS = 2 * 24 * 60 * 60


def attendance_channel() -> None:
    """
    Server script to post the attendance summary to the Slack channel
    Enqueues the background job to post the message
    Conditions:
     - Check send attendance updates is enabled
     - Check if the current date is a working day (weekends, holidays)
     - Check if current date notification is sent
     - If not, send the notification, set the updated date in Slack Settings
    """
    slack_settings = frappe.get_single("Slack Settings")

    current_date = frappe.utils.nowdate()
    current_day = datetime.strptime(current_date, "%Y-%m-%d").weekday()
    if (
        slack_settings.send_attendance_updates != 1
        or current_day > 4  # sat = 5, sun = 6
        or is_holiday(current_date)
        or (
            slack_settings.last_attendance_date is not None
            and slack_settings.last_attendance_date == frappe.utils.nowdate()
        )
        or frappe.utils.now_datetime().time() < get_time(slack_settings.attendance_time)
    ):
        return

    # Send the attendance summary to the Slack channel
    message_ts = send_notification(get_attendance_title(slack_settings))

    # Stamp the day with a direct write: a full save() would write back every
    # column of the in-memory doc loaded before the post, overwriting whatever
    # another process changed in Slack Settings meanwhile
    frappe.db.set_single_value(
        "Slack Settings",
        {"last_attendance_date": frappe.utils.nowdate(), "last_attendance_msg_ts": message_ts},
    )


def get_attendance_title(slack_settings) -> str:
    """
    Title used for the attendance summary header, falling back to a default
    when Slack Settings has no leave notification subject
    """
    return slack_settings.leave_notification_subject or "Employees on Leave"


def updated_at_context_block(updated_at: str) -> dict:
    """
    Trailing context block that marks an in-place edit; editing a message
    does not notify anyone, so this is how readers can tell it changed
    """
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_Updated at {updated_at}_"}],
    }


def attendance_blocks_hash(blocks: list) -> str:
    """
    Fingerprint of the summary content, used to skip a Slack edit that
    would change nothing (for example the later jobs of a bulk reject, each
    of which rebuilds the same list). Callers hash the content blocks before
    appending the "Updated at" block, so the time of an edit is not a change
    """
    return hashlib.sha256(json.dumps(blocks, sort_keys=True, default=str).encode()).hexdigest()


def _attendance_hash_key(message_ts: str) -> str:
    return f"{ATTENDANCE_HASH_CACHE_PREFIX}:{message_ts}"


def get_attendance_hash(message_ts: str) -> str | None:
    """
    Fingerprint of what the summary message ``message_ts`` currently shows,
    or None when unknown (never recorded, expired, or the cache was flushed)
    """
    return frappe.cache.get_value(_attendance_hash_key(message_ts))


def remember_attendance_hash(message_ts: str, content_hash: str) -> None:
    """
    Record what Slack now shows for ``message_ts``, right after Slack
    accepted the post or edit. Lives in the cache, not in Slack Settings:
    a document write would bump its ``modified`` on every refresh and make
    any long-running job that later saves Slack Settings fail its timestamp
    check. Never raises: losing the fingerprint only costs one redundant edit
    """
    try:
        frappe.cache.set_value(
            _attendance_hash_key(message_ts), content_hash, expires_in_sec=ATTENDANCE_HASH_TTL_SECONDS
        )
    except Exception as e:
        generate_error_log(
            title=_("Error remembering the attendance summary fingerprint"),
            exception=e,
        )


def build_attendance_blocks(attendance_title: str, *, updated_at: str | None = None) -> list:
    """
    Build the Slack blocks for today's attendance summary
    Runs the leave query, groups the employees by leave type and formats
    the result. When ``updated_at`` is given a trailing context block is
    appended so readers can tell the message was edited in place; this is
    the only difference between the morning post and an in-place update.
    """
    mention_users = frappe.db.get_single_value("Slack Settings", "mention_user")
    leave_groups = {"Full Day": [], "Half Day": []}
    if custom_fields_exist():
        leave_groups["First-Half"] = []
        leave_groups["Second-Half"] = []

    users_on_leave = get_employees_on_leave()

    if users_on_leave:
        # Batch fetch employee user_ids
        employee_ids = [u.get("employee") for u in users_on_leave]
        employee_user_ids = frappe.get_all(
            "Employee",
            filters={"name": ["in", employee_ids]},
            fields=["name", "user_id"],
        )
        employee_to_user = {e.name: e.user_id for e in employee_user_ids}

        # Batch fetch User Meta for all users
        user_ids = [u for u in employee_to_user.values() if u]
        user_metas = (
            frappe.get_all(
                "User Meta",
                filters={"user": ["in", user_ids]},
                fields=["user", "custom_slack_userid"],
            )
            if user_ids
            else []
        )
        user_to_slack = {um.user: um.custom_slack_userid for um in user_metas}

    for user_application in users_on_leave:
        employee_id = user_application.get("employee")
        user_id = employee_to_user.get(employee_id)
        slack_userid = user_to_slack.get(user_id) if user_id else None

        slack_name = f"<@{slack_userid}>" if slack_userid and mention_users else user_application.employee_name

        leave_type = get_leave_type(user_application)
        leave_info = {
            "name": slack_name,
            "until_date": (
                user_application.to_date
                # Don't show until date if the leave ends today
                if user_application.to_date != getdate(frappe.utils.nowdate())
                else None
            ),
        }
        leave_groups[leave_type].append(leave_info)

    blocks = format_attendance_blocks(
        date_string=standard_date_fmt(frappe.utils.nowdate()),
        attendance_title=attendance_title,
        employee_count=len(users_on_leave),
        leave_details_mrkdwn=format_leave_groups(leave_groups),
    )

    if updated_at:
        blocks.append(updated_at_context_block(updated_at))

    return blocks


def send_notification(attendance_title: str) -> str | None:
    """
    Background job to post the attendance summary to the Slack channel
    Returns the message timestamp if successful
    """
    slack = SlackIntegration()
    blocks = build_attendance_blocks(attendance_title)

    try:
        message = slack.slack_app.client.chat_postMessage(
            channel=slack.SLACK_CHANNEL_ID,
            blocks=blocks,
        )
    except Exception as e:
        generate_error_log(
            title=_("Error posting message to Slack"),
            message=_("Please check the channel ID and try again."),
            exception=e,
            msgprint=True,
            realtime=True,
        )
        return None

    # Remember what was posted so a later refresh with the same content can
    # skip the edit
    remember_attendance_hash(message["ts"], attendance_blocks_hash(blocks))
    return message["ts"]


def update_attendance_summary() -> None:
    """
    Background job to rebuild today's attendance summary and edit the
    already-posted Slack message in place
    Runs when a leave covering today changes after the morning post (for
    example it is rejected or cancelled, or a new one is applied for), so
    the list and the header count stay accurate for the rest of the day.
    Does nothing when today's summary has not been posted yet: the morning
    post will pick up the current state on its own.

    The whole job (read, build, compare, edit) runs under a site-level file
    lock so that concurrent refreshes (for example a bulk reject queues one
    job per leave) cannot interleave and let an older read overwrite a newer
    edit. Within the lock the rebuilt content is compared with the cached
    fingerprint of what the message currently shows; when they match (the
    earlier job of that burst already caught up with every change) the Slack
    call is skipped. The fingerprint is recorded right after Slack accepts
    the edit, still inside the lock, so the next job compares against what
    Slack really shows.
    """
    with filelock(ATTENDANCE_UPDATE_LOCK, timeout=60):
        slack_settings = frappe.get_single("Slack Settings")
        if (
            slack_settings.send_attendance_updates != 1
            or not slack_settings.last_attendance_msg_ts
            or not slack_settings.last_attendance_date
            or getdate(slack_settings.last_attendance_date) != getdate(today())
        ):
            return

        slack = SlackIntegration()
        if not slack.SLACK_CHANNEL_ID:
            return

        message_ts = slack_settings.last_attendance_msg_ts
        blocks = build_attendance_blocks(get_attendance_title(slack_settings))
        content_hash = attendance_blocks_hash(blocks)
        if content_hash == get_attendance_hash(message_ts):
            return

        blocks.append(updated_at_context_block(frappe.utils.now_datetime().strftime("%H:%M")))

        try:
            slack.slack_app.client.chat_update(
                channel=slack.SLACK_CHANNEL_ID,
                ts=message_ts,
                blocks=blocks,
            )
        except Exception as e:
            generate_error_log(
                title=_("Error updating attendance summary in Slack"),
                message=_("Please check the channel ID and try again."),
                exception=e,
            )
            return

        remember_attendance_hash(message_ts, content_hash)


def get_leave_type(user_application: dict) -> str:
    """
    Get the leave type based on the user's leave application
    For standalone installations, the custom fields are not available,
    so only use Full Day, and Half Day
    For rtCamp installation, use Full Day, First-Half, and Second-Half
    """
    if not user_application.half_day or user_application.half_day_date != getdate(today()):
        return "Full Day"
    elif not custom_fields_exist():
        return "Half Day"
    elif user_application.custom_first_halfsecond_half == "First Half":
        return "First-Half"
    else:
        return "Second-Half"


def format_leave_groups(leave_groups: dict) -> str:
    """
    Format the leave groups into a readable text for posting to Slack
    """
    formatted_text = ""

    for leave_type, employees in leave_groups.items():
        if not employees:
            continue

        formatted_text += f"*{leave_type}*\n"
        for index, employee in enumerate(employees, start=1):
            formatted_text += f"  {index}. {employee['name']}"
            if employee["until_date"]:
                formatted_text += f" _until {standard_date_fmt(employee['until_date'])}_"
            formatted_text += "\n"
        formatted_text += "\n"

    return formatted_text.strip()


def format_attendance_blocks(
    *,
    date_string: str,
    employee_count: int,
    leave_details_mrkdwn: str,
    attendance_title: str,
) -> list:
    """
    Format the attendance summary into Slack blocks
    """
    if employee_count == 0:
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":sunny: No {attendance_title}",
                    "emoji": True,
                },
            }
        ]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":palm_tree: {employee_count} {attendance_title}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": leave_details_mrkdwn,
            },
        },
    ]

    return blocks

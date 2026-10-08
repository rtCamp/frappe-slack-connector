import frappe
from frappe.model.document import Document
from frappe.utils import flt, get_url_to_form, getdate

from frappe_slack_connector.db.leave_application import custom_fields_exist
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


def after_insert(doc, method):
    """
    Send a slack message to the leave approver when a new leave application
    is submitted
    """
    frappe.enqueue(
        send_leave_notification_bg,
        queue="short",
        doc=doc,
    )
    frappe.enqueue(
        send_leave_notification_to_applicant,
        queue="short",
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
        day_period = format_leave_duration(doc)

        # Leave Without Pay types have no allocation, so the balance is meaningless
        leave_balance = None
        if doc.leave_balance is not None and not frappe.db.get_value("Leave Type", doc.leave_type, "is_lwp"):
            leave_balance = flt(doc.leave_balance)

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
                    duration=day_period,
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


def format_leave_duration(doc: Document) -> str:
    """
    Describe how much of the leave is taken: "Full Day", the half for a
    single-day half-day leave, or the total days plus the half-day date
    for a multi-day leave that includes one half day
    """
    if not doc.half_day:
        return "Full Day"

    period = doc.custom_first_halfsecond_half if custom_fields_exist() else None
    if getdate(doc.from_date) == getdate(doc.to_date):
        return period or "Half Day"

    half_day_on = f"half day on {standard_date_fmt(doc.half_day_date)}" if doc.half_day_date else "one half day"
    return f"{flt(doc.total_leave_days):g} days ({half_day_on}{', ' + period if period else ''})"


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

    `leave_balance` is the balance recorded on the application when it was
    submitted; pass None to omit it (e.g. for Leave Without Pay)
    """
    details = [{"type": "mrkdwn", "text": f"*Duration:*\n:hourglass_flowing_sand: {duration}"}]
    if total_days is not None:
        details.append({"type": "mrkdwn", "text": f"*Requested:*\n{flt(total_days):g} day(s)"})
    if leave_balance is not None:
        details.append(
            {
                "type": "mrkdwn",
                "text": f"*Balance before this request:*\n{flt(leave_balance):g} day(s)",
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
    if total_days is not None and leave_balance is not None and flt(total_days) > flt(leave_balance):
        shortfall = flt(flt(total_days) - flt(leave_balance), 2)
        resulting_balance = flt(flt(leave_balance) - flt(total_days), 2)
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

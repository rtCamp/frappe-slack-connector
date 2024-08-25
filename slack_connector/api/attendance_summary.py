import frappe
from frappe import _

from slack_connector.db.leave_application import get_employees_on_leave
from slack_connector.db.user_meta import get_user_meta
from slack_connector.helpers.standard_date import standard_date_fmt
from slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
def attendance_channel() -> None:
    frappe.enqueue(attendance_channel_bg, queue="short")


def attendance_channel_bg() -> None:
    slack = SlackIntegration()
    announcement = ""
    users_on_leave = get_employees_on_leave()

    for user_application in users_on_leave:
        user_slack = get_user_meta(employee_id=user_application.get("employee"))
        slack_name = (
            f"<@{user_slack.custom_slack_userid}>"
            if user_slack
            else user_application.employee_name
        )
        announcement += (
            f"\n{slack_name} "
            f"_{'Half Day' if user_application.half_day else 'Full Day'}"
            f"{', (Unapproved)' if user_application.status != 'Approved' else ''}_"
        )

    try:
        slack.slack_app.client.chat_postMessage(
            channel=slack.SLACK_CHANNEL_ID,
            blocks=format_attendance_blocks(
                date_string=standard_date_fmt(frappe.utils.nowdate()),
                employee_count=len(users_on_leave),
                leave_details_mrkdwn=announcement,
            ),
        )
    except Exception as e:
        frappe.log_error(title="Error posting message to Slack", message=str(e))
        frappe.msgprint(
            title=_("Error posting message to Slack"),
            msg=_("Please check the channel ID and try again."),
            realtime=True,
        )


def format_attendance_blocks(
    date_string: str, employee_count: int, leave_details_mrkdwn: str
) -> list:
    blocks = [
        # {
        #     "type": "image",
        #     "image_url": "https://api.slack.com/img/blocks/bkb_template_images/notifications.png",
        #     "alt_text": "Calendar illustration",
        # },
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":calendar: Daily Leave Notification",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Date:*\n{date_string}"},
                {
                    "type": "mrkdwn",
                    "text": f"*On Leave:*\n{employee_count} {('employee' if employee_count <= 1 else 'employees')}",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":busts_in_silhouette: *Employees on Leave:*",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":information_source: _Full Day :full_moon: and Half Day :last_quarter_moon: leaves are marked accordingly_",
                }
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": leave_details_mrkdwn}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "For any queries, please contact HR department.",
                }
            ],
        },
    ]

    return blocks


def format_attendance_blocks_minimalistic(
    date_string: str, employee_count: int, leave_details_mrkdwn: str
) -> list:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Daily Leave Notification*\n\n"
                    f"Today: {date_string}\n{employee_count} {('employee' if employee_count <= 1 else 'employees')}"
                ),
            },
            "accessory": {
                "type": "image",
                "image_url": "https://api.slack.com/img/blocks/bkb_template_images/notifications.png",
                "alt_text": "calendar thumbnail",
            },
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*On leave today:*"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": leave_details_mrkdwn},
        },
    ]

    return blocks

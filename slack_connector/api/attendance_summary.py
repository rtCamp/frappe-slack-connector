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
            f"\n{':last_quarter_moon:' if user_application.half_day else ':full_moon:'}  "
            f"{slack_name} "
            f"_{'until ' + standard_date_fmt(user_application.to_date) if user_application.from_date != user_application.to_date else '' } "
            f"{'(Unapproved)' if user_application.status != 'Approved' else ''}_"
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
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":calendar: {employee_count} {('employee' if employee_count <= 1 else 'employees')} on leave today",
                "emoji": True,
            },
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
                    "text": "_Full Day :full_moon: and Half Day :last_quarter_moon: leaves are marked accordingly_",
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
                    "text": "Thank you for your attention :pray:",
                }
            ],
        },
    ]

    return blocks

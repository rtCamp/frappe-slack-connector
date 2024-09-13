from datetime import datetime

import frappe
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from frappe import _
from frappe.utils import get_time, getdate

from frappe_slack_connector.db.leave_application import get_employees_on_leave
from frappe_slack_connector.db.user_meta import get_user_meta
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist()
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
    send_notification()

    # Update the last attendance date
    slack_settings.last_attendance_date = frappe.utils.nowdate()
    slack_settings.save(ignore_permissions=True)


def send_notification() -> None:
    """
    Background job to post the attendance summary to the Slack channel
    """
    slack = SlackIntegration()
    leave_groups = {"Full Day": [], "First-Half": [], "Second-Half": []}
    users_on_leave = get_employees_on_leave()

    for user_application in users_on_leave:
        user_slack = get_user_meta(employee_id=user_application.get("employee"))
        slack_name = (
            f"<@{user_slack.custom_slack_userid}>"
            if user_slack and user_slack.custom_slack_userid
            else user_application.employee_name
        )

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

    leave_details_mrkdwn = format_leave_groups(leave_groups)

    try:
        slack.slack_app.client.chat_postMessage(
            channel=slack.SLACK_CHANNEL_ID,
            blocks=format_attendance_blocks(
                date_string=standard_date_fmt(frappe.utils.nowdate()),
                employee_count=len(users_on_leave),
                leave_details_mrkdwn=leave_details_mrkdwn,
            ),
        )
    except Exception as e:
        generate_error_log(
            title=_("Error posting message to Slack"),
            message=_("Please check the channel ID and try again."),
            exception=e,
            msgprint=True,
            realtime=True,
        )


def get_leave_type(user_application: dict) -> str:
    """
    Get the leave type based on the user's leave application
    """
    if not user_application.half_day:
        return "Full Day"
    elif user_application.custom_first_halfsecond_half == "First Half":
        return "First-Half"
    else:
        return "Second-Half"


def format_leave_groups(leave_groups: dict) -> str:
    """
    Format the leave groups into a readable text for posting to Slack
    """
    formatted_text = ""
    emojis = {"Full Day": "", "First-Half": "", "Second-Half": ""}

    for leave_type, employees in leave_groups.items():
        if employees:
            formatted_text += f"*{emojis[leave_type]} {leave_type}*\n"
            for index, employee in enumerate(employees, start=1):
                formatted_text += f"  {index}. {employee['name']}"
                if employee["until_date"]:
                    formatted_text += (
                        f" _until {standard_date_fmt(employee['until_date'])}_"
                    )
                formatted_text += "\n"
            formatted_text += "\n"

    return formatted_text.strip()


def format_attendance_blocks(
    date_string: str,
    employee_count: int,
    leave_details_mrkdwn: str,
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
                    "text": ":sunny: No rtCampers are on leave today",
                    "emoji": True,
                },
            }
        ]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":palm_tree: {employee_count} rtCamper{('' if employee_count <= 1 else 's')} on leave today",
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

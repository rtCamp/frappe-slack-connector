import frappe
from frappe.utils import getdate

from frappe_slack_connector.db.timesheet import create_timesheet_detail
from frappe_slack_connector.db.user_meta import (
    get_employeeid_from_slackid,
    get_userid_from_slackid,
)
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.helpers.str_utils import strip_html_tags
from frappe_slack_connector.slack.app import SlackIntegration


def handler(slack: SlackIntegration, payload: dict):
    """
    Handle the timesheet submission interaction
    """
    if not payload:
        frappe.throw("No payload found", frappe.ValidationError)

    try:
        user_info = payload["user"]
        view_state = payload["view"]["state"]["values"]
        employee = get_employeeid_from_slackid(user_info["id"])

        # Set the user performing the action
        frappe.set_user(get_userid_from_slackid(user_info["id"]))

        task = view_state["task_block"]["task_select"]["selected_option"]["value"]
        date = view_state["entry_date"]["date_picker"]["selected_date"]
        description = view_state["description"]["description_input"]["value"]
        hours = float(view_state["hours_block"]["hours_input"]["value"])
        if not task:
            raise Exception("Task is mandatory.")

        project = frappe.get_value("Task", task, "project")
        parent = frappe.db.get_value(
            "Timesheet",
            {
                "employee": employee,
                "start_date": [">=", getdate(date)],
                "end_date": ["<=", getdate(date)],
                "parent_project": project,
                "docstatus": ["!=", 2],
            },
            "name",
        )
        create_timesheet_detail(date, hours, description, task, employee, parent)
        response = {
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Submitted"},
                "close": {"type": "plain_text", "text": "Close"},
                "clear_on_close": True,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":white_check_mark: Timesheet submitted successfully",
                            "emoji": True,
                        },
                    },
                ],
            },
        }
        return send_http_response(
            body=response,
            status_code=200,
        )

    except Exception as e:
        if not e:
            e = frappe.get_traceback()
        generate_error_log("Error submitting timesheet", exception=e)
        response = {
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Error"},
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":warning: Error submitting timesheet",
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Error Details:*\n```{strip_html_tags(str(e))}```",
                        },
                    },
                ],
            },
        }
        return send_http_response(
            body=response,
        )

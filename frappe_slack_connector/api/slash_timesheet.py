import frappe

from frappe_slack_connector.db.user_meta import get_userid_from_slackid
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist(allow_guest=True)
def slash_timesheet():
    """
    API endpoint for the Slash command to open the modal for timesheet creation
    Slash command: /timesheet
    """
    slack = SlackIntegration()
    try:
        slack.verify_slack_request(
            signature=frappe.request.headers.get("X-Slack-Signature"),
            timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
            req_data=frappe.request.get_data(as_text=True),
        )
    except Exception:
        return send_http_response("Invalid request", status_code=403)
    try:
        user_email = get_userid_from_slackid(frappe.form_dict.get("user_id"))
        if user_email is None:
            raise Exception("User not found on ERP")

        projects = frappe.get_all(
            "Project",
            filters={"status": "Open"},
            fields=["name", "project_name"],
            user=user_email,
        )
        tasks = frappe.get_list(
            "Task",
            user=user_email,
            fields=["name", "subject"],
            ignore_permissions=True,
        )

        slack.slack_app.client.views_open(
            trigger_id=frappe.form_dict.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": "timesheet_modal",
                "title": {"type": "plain_text", "text": "Timesheet Entry"},
                "blocks": build_timesheet_form(projects, tasks),
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "submit": {
                    "type": "plain_text",
                    "text": "Submit",
                },
            },
        )

    except Exception as e:
        generate_error_log("Error opening modal", exception=e)
        slack.slack_app.client.views_open(
            trigger_id=frappe.form_dict.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": "timesheet_error",
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
                            "text": f"*Error Details:*\n```{str(e)}```",
                        },
                    },
                ],
            },
        )

    return send_http_response(
        status_code=204,
        is_empty=True,
    )


def build_timesheet_form(projects: list, tasks: list) -> list:
    blocks = [
        {
            "type": "input",
            "block_id": "entry_date",
            "element": {
                "type": "datepicker",
                "action_id": "date_picker",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a date",
                },
                "initial_date": frappe.utils.today(),
            },
            "label": {"type": "plain_text", "text": "Date"},
        },
        {
            "type": "input",
            "block_id": "project_block",
            "element": {
                "type": "static_select",
                "action_id": "project_select",
                "options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": project.get("project_name"),
                        },
                        "value": project.get("name"),
                    }
                    for project in projects
                ],
                "placeholder": {"type": "plain_text", "text": "Enter project name"},
            },
            "label": {"type": "plain_text", "text": "Project", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "task_block",
            "element": {
                "type": "static_select",
                "action_id": "task_select",
                "options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": task.get("subject"),
                        },
                        "value": task.get("name"),
                    }
                    for task in tasks
                ],
                "placeholder": {
                    "type": "plain_text",
                    "text": "Enter task description",
                },
            },
            "label": {"type": "plain_text", "text": "Task", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "hours_block",
            "element": {
                "type": "number_input",
                "action_id": "hours_input",
                "is_decimal_allowed": True,
                "min_value": "0.1",
                "placeholder": {"type": "plain_text", "text": "Enter hours worked"},
            },
            "label": {"type": "plain_text", "text": "Hours", "emoji": True},
        },
        {
            "type": "input",
            "block_id": "description",
            "element": {
                "type": "plain_text_input",
                "action_id": "description_input",
                "multiline": True,
            },
            "label": {"type": "plain_text", "text": "Description", "emoji": True},
        },
    ]
    return blocks

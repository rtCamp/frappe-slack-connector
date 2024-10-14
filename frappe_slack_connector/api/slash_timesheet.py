import frappe

from frappe_slack_connector.db.timesheet import get_user_projects, get_user_tasks
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
    slack_userid = frappe.form_dict.get("user_id")
    slack_trigger_id = frappe.form_dict.get("trigger_id")

    try:
        slack.verify_slack_request(
            signature=frappe.request.headers.get("X-Slack-Signature"),
            timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
            req_data=frappe.request.get_data(as_text=True),
        )
    except Exception:
        return send_http_response("Invalid request", status_code=403)
    try:
        user_email = get_userid_from_slackid(slack_userid)
        if user_email is None:
            raise Exception("User not found on ERP")

        # NOTE: `set_user()` will effectively wipe out frappe.form_dict
        frappe.set_user(user_email)
        projects = get_user_projects(user_email)
        tasks = get_user_tasks(user_email)

        if not projects:
            raise Exception("No projects found")
        if not tasks:
            raise Exception("No tasks found")

        slack.slack_app.client.views_open(
            trigger_id=slack_trigger_id,
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
        exc = str(e) if str(e) else "There was an error opening the timesheet modal"

        slack.slack_app.client.views_open(
            trigger_id=slack_trigger_id,
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
                            "text": f"*Error Details:*\n```{exc}```",
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
    """
    Build the form for the timesheet modal
    Provide options for project and task
    """

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
            "dispatch_action": True,
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
            "dispatch_action": True,
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

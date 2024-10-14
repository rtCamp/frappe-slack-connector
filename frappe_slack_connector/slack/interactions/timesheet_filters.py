import frappe

from frappe_slack_connector.db.timesheet import get_user_tasks
from frappe_slack_connector.db.user_meta import get_userid_from_slackid
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.str_utils import strip_html_tags
from frappe_slack_connector.slack.app import SlackIntegration


def handle_timesheet_filter(slack: SlackIntegration, payload: dict):
    """
    Handle the timesheet project and task selection interactions
    """
    try:
        user = get_userid_from_slackid(payload["user"]["id"])
        # NOTE: `set_user()` will effectively wipe out frappe.form_dict
        frappe.set_user(user)

        action_id = payload["actions"][0]["action_id"]
        if action_id == "project_select":
            return handle_project_select(slack, payload)
        elif action_id == "task_select":
            return handle_task_select(slack, payload)
    except Exception as e:
        exc = str(e)
        if not exc:
            exc = "There was an error. Please check ERP dashboard"
            generate_error_log(
                "Error getting projects for timesheet", message=frappe.get_traceback()
            )
        slack.slack_app.client.views_push(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "timesheet_modal",
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
                            "text": f"```{strip_html_tags(exc)}```",
                        },
                    },
                ],
            },
        )


def handle_project_select(slack: SlackIntegration, payload: dict):
    """
    Handle the project selection interaction
    Update the task block with the tasks for the selected project
    """
    view = payload["view"]
    state = view["state"]["values"]
    blocks = view["blocks"]

    user = get_userid_from_slackid(payload["user"]["id"])

    project = state["project_block"]["project_select"]["selected_option"]["value"]
    tasks = get_user_tasks(user, project)

    # If no tasks found for the project, show an error message
    if not tasks:
        slack.slack_app.client.views_push(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "timesheet_modal",
                "title": {"type": "plain_text", "text": "Error"},
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":warning: No tasks found for the project",
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"```No tasks found for the project {project}```",
                        },
                    },
                ],
            },
        )
        return

    # Update the task block with the tasks for the selected project
    for block in blocks:
        if block["block_id"] == "task_block":
            block["element"]["options"] = [
                {
                    "text": {
                        "type": "plain_text",
                        "text": task.get("subject"),
                    },
                    "value": task.get("name"),
                }
                for task in tasks
            ]
            break

    # Update the modal with the modified blocks
    updated_view = {
        "type": "modal",
        "callback_id": view["callback_id"],
        "title": view["title"],
        "submit": view["submit"],
        "blocks": blocks,
    }

    # Use views_update to update the modal
    slack.slack_app.client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=updated_view,
    )


def handle_task_select(slack: SlackIntegration, payload: dict):
    """
    Handle the task selection interaction
    Set the project block with the project for the selected task
    Required when the project is not selected, directly selecting the task
    """
    view = payload["view"]
    state = view["state"]["values"]
    blocks = view["blocks"]

    task = state["task_block"]["task_select"]["selected_option"]["value"]
    project = frappe.get_value("Task", task, "project")
    project_id, project_name = frappe.get_value(
        "Project", project, ["name", "project_name"]
    )

    # Set the initial project option in the project block
    for block in blocks:
        if block["block_id"] == "project_block":
            block["element"]["initial_option"] = {
                "text": {
                    "type": "plain_text",
                    "text": project_name,
                },
                "value": project_id,
            }
            break

    # Update the modal with the modified blocks
    updated_view = {
        "type": "modal",
        "callback_id": view["callback_id"],
        "title": view["title"],
        "submit": view["submit"],
        "blocks": blocks,
    }

    # Use views_update to update the modal
    slack.slack_app.client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=updated_view,
    )

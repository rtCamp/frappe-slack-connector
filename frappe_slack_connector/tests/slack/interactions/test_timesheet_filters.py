from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.timesheet_filters import (
    handle_project_select,
    handle_task_select,
)
from frappe_slack_connector.tests import TEST_SLACK_USER_ID, TEST_USER

FILTERS_MODULE = "frappe_slack_connector.slack.interactions.timesheet_filters"


def _build_filter_payload(*, selected_project="PRJ-1", selected_task=None):
    """Build a Slack block_actions payload for the timesheet modal filters."""
    state = {
        "project_block": {"project_select": {"selected_option": {"value": selected_project}}},
    }
    if selected_task:
        state["task_block"] = {"task_select": {"selected_option": {"value": selected_task}}}
    return {
        "user": {"id": TEST_SLACK_USER_ID},
        "trigger_id": "T-trigger-1",
        "view": {
            "id": "V001",
            "hash": "H001",
            "callback_id": "timesheet_modal",
            "title": {"type": "plain_text", "text": "Timesheet Entry"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {"block_id": "entry_date", "type": "input"},
                {
                    "block_id": "project_block",
                    "type": "input",
                    "element": {"type": "static_select", "options": []},
                },
                {
                    "block_id": "task_block",
                    "type": "input",
                    "element": {"type": "static_select", "options": []},
                },
                {"block_id": "hours_block", "type": "input"},
                {"block_id": "description", "type": "input"},
            ],
            "state": {"values": state},
        },
    }


class TestHandleProjectSelect(IntegrationTestCase):
    def test_updates_task_block_options_for_selected_project(self):
        """handle_project_select re-queries tasks for the chosen project and calls views_update with the refreshed task options."""
        payload = _build_filter_payload(selected_project="PRJ-9")
        mock_slack = MagicMock()
        with (
            patch(f"{FILTERS_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(
                f"{FILTERS_MODULE}.get_user_tasks",
                return_value=[
                    {"name": "T-A", "subject": "Task A"},
                    {"name": "T-B", "subject": "Task B"},
                ],
            ) as mock_tasks,
        ):
            handle_project_select(mock_slack, payload)
        mock_tasks.assert_called_once_with(TEST_USER, "PRJ-9")
        mock_slack.slack_app.client.views_update.assert_called_once()
        view = mock_slack.slack_app.client.views_update.call_args.kwargs["view"]
        task_block = next(b for b in view["blocks"] if b["block_id"] == "task_block")
        task_values = [o["value"] for o in task_block["element"]["options"]]
        self.assertEqual(task_values, ["T-A", "T-B"])

    def test_pushes_error_modal_when_no_tasks_found(self):
        """handle_project_select pushes an error modal via views_push when get_user_tasks returns an empty list."""
        payload = _build_filter_payload(selected_project="PRJ-EMPTY")
        mock_slack = MagicMock()
        with (
            patch(f"{FILTERS_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{FILTERS_MODULE}.get_user_tasks", return_value=[]),
        ):
            handle_project_select(mock_slack, payload)
        mock_slack.slack_app.client.views_push.assert_called_once()
        mock_slack.slack_app.client.views_update.assert_not_called()


class TestHandleTaskSelect(IntegrationTestCase):
    def test_sets_project_initial_option_from_tasks_project(self):
        """handle_task_select looks up the project for the selected task and sets it as the initial_option on the project block."""
        payload = _build_filter_payload(selected_project="PRJ-OLD", selected_task="TASK-X")
        mock_slack = MagicMock()
        with patch(
            f"{FILTERS_MODULE}.frappe.get_value",
            side_effect=["PRJ-NEW", ("PRJ-NEW", "Project New")],
        ):
            handle_task_select(mock_slack, payload)
        view = mock_slack.slack_app.client.views_update.call_args.kwargs["view"]
        project_block = next(b for b in view["blocks"] if b["block_id"] == "project_block")
        initial = project_block["element"]["initial_option"]
        self.assertEqual(initial["value"], "PRJ-NEW")
        self.assertEqual(initial["text"]["text"], "Project New")

    def test_raises_when_project_not_found_for_task(self):
        """handle_task_select raises when frappe.get_value cannot resolve the project from the task."""
        payload = _build_filter_payload(selected_project="PRJ-OLD", selected_task="TASK-Y")
        mock_slack = MagicMock()
        with patch(f"{FILTERS_MODULE}.frappe.get_value", side_effect=["PRJ-NEW", None]):
            with self.assertRaises(Exception):
                handle_task_select(mock_slack, payload)

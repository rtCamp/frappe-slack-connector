from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.timesheet_modal import (
    show_timesheet_modal,
)
from frappe_slack_connector.tests import TEST_SLACK_USER_ID, TEST_USER

TIMESHEET_MODAL_MODULE = "frappe_slack_connector.slack.interactions.timesheet_modal"


class TestShowTimesheetModal(IntegrationTestCase):
    def test_opens_modal_with_date_project_task_hours_description_blocks(self):
        """show_timesheet_modal opens a modal whose blocks include entry_date, project_block, task_block, hours_block, description."""
        mock_slack = MagicMock()
        with (
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{TIMESHEET_MODAL_MODULE}.frappe.set_user"),
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_user_projects",
                return_value=[{"name": "PRJ-1", "project_name": "Project One"}],
            ),
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_user_tasks",
                return_value=[{"name": "TASK-1", "subject": "Task One"}],
            ),
        ):
            show_timesheet_modal(mock_slack, TEST_SLACK_USER_ID, "T-trigger-1")
        mock_slack.slack_app.client.views_open.assert_called_once()
        view = mock_slack.slack_app.client.views_open.call_args.kwargs["view"]
        self.assertEqual(view["callback_id"], "timesheet_modal")
        block_ids = [b.get("block_id") for b in view["blocks"]]
        for required in (
            "entry_date",
            "project_block",
            "task_block",
            "hours_block",
            "description",
        ):
            self.assertIn(required, block_ids)

    def test_passes_default_limit_of_99_to_get_user_projects(self):
        """show_timesheet_modal queries projects via get_user_projects with the default (99) limit."""
        mock_slack = MagicMock()
        with (
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{TIMESHEET_MODAL_MODULE}.frappe.set_user"),
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_user_projects",
                return_value=[{"name": "PRJ-1", "project_name": "Project"}],
            ) as mock_projects,
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_user_tasks",
                return_value=[{"name": "TASK-1", "subject": "Task"}],
            ),
        ):
            show_timesheet_modal(mock_slack, TEST_SLACK_USER_ID, "T-trigger-2")
        mock_projects.assert_called_once_with(TEST_USER)

    def test_opens_error_modal_when_employee_lookup_fails(self):
        """show_timesheet_modal opens an error modal when get_userid_from_slackid returns None."""
        mock_slack = MagicMock()
        with (
            patch(f"{TIMESHEET_MODAL_MODULE}.get_userid_from_slackid", return_value=None),
            patch(f"{TIMESHEET_MODAL_MODULE}.generate_error_log"),
        ):
            show_timesheet_modal(mock_slack, TEST_SLACK_USER_ID, "T-trigger-3")
        mock_slack.slack_app.client.views_open.assert_called_once()
        view = mock_slack.slack_app.client.views_open.call_args.kwargs["view"]
        self.assertEqual(view["callback_id"], "timesheet_error")

    def test_opens_error_modal_when_no_projects_returned(self):
        """show_timesheet_modal opens an error modal when get_user_projects returns an empty list."""
        mock_slack = MagicMock()
        with (
            patch(
                f"{TIMESHEET_MODAL_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{TIMESHEET_MODAL_MODULE}.frappe.set_user"),
            patch(f"{TIMESHEET_MODAL_MODULE}.get_user_projects", return_value=[]),
            patch(f"{TIMESHEET_MODAL_MODULE}.generate_error_log"),
        ):
            show_timesheet_modal(mock_slack, TEST_SLACK_USER_ID, "T-trigger-4")
        view = mock_slack.slack_app.client.views_open.call_args.kwargs["view"]
        self.assertEqual(view["callback_id"], "timesheet_error")

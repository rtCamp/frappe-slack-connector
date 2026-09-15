from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.submit_timesheet import handler
from frappe_slack_connector.tests import TEST_SLACK_USER_ID, TEST_USER

SUBMIT_TIMESHEET_MODULE = "frappe_slack_connector.slack.interactions.submit_timesheet"


def _build_submission_payload(
    *,
    task="TASK-1",
    date="2026-06-10",
    description="worked on api",
    hours="2.5",
):
    """Build a Slack view_submission payload mimicking the timesheet-modal form fields."""
    return {
        "user": {"id": TEST_SLACK_USER_ID},
        "view": {
            "state": {
                "values": {
                    "task_block": {"task_select": {"selected_option": {"value": task}}},
                    "entry_date": {"date_picker": {"selected_date": date}},
                    "description": {"description_input": {"value": description}},
                    "hours_block": {"hours_input": {"value": hours}},
                }
            }
        },
    }


class TestSubmitTimesheetHandler(IntegrationTestCase):
    def test_calls_create_timesheet_detail_with_parsed_fields(self):
        """handler parses task, date, description, hours from view.state.values and calls create_timesheet_detail with them."""
        payload = _build_submission_payload(task="TASK-9", date="2026-06-11", description="coding", hours="3.5")
        with (
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.set_user"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.get_value", return_value="PRJ-1"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.db.get_value", return_value=None),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.create_timesheet_detail") as mock_create,
            patch(f"{SUBMIT_TIMESHEET_MODULE}.send_http_response"),
        ):
            handler(slack=MagicMock(), payload=payload)
        mock_create.assert_called_once_with("2026-06-11", 3.5, "coding", "TASK-9", "EMP-001", None)

    def test_returns_success_response_when_submission_succeeds(self):
        """handler returns an HTTP response with a success modal when create_timesheet_detail completes."""
        with (
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.set_user"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.get_value", return_value="PRJ-1"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.db.get_value", return_value=None),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.create_timesheet_detail"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.send_http_response") as mock_response,
        ):
            handler(slack=MagicMock(), payload=_build_submission_payload())
        body = mock_response.call_args.kwargs["body"]
        self.assertEqual(body["response_action"], "push")
        self.assertEqual(body["view"]["title"]["text"], "Submitted")

    def test_returns_error_modal_when_create_timesheet_detail_raises(self):
        """handler returns an error-modal response when create_timesheet_detail raises."""
        with (
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.get_userid_from_slackid",
                return_value=TEST_USER,
            ),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.set_user"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.get_value", return_value="PRJ-1"),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.frappe.db.get_value", return_value=None),
            patch(
                f"{SUBMIT_TIMESHEET_MODULE}.create_timesheet_detail",
                side_effect=RuntimeError("boom"),
            ),
            patch(f"{SUBMIT_TIMESHEET_MODULE}.send_http_response") as mock_response,
        ):
            handler(slack=MagicMock(), payload=_build_submission_payload())
        body = mock_response.call_args.kwargs["body"]
        self.assertEqual(body["view"]["title"]["text"], "Error")

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.slash_timesheet import slash_timesheet
from frappe_slack_connector.tests import TEST_SLACK_USER_ID

SLASH_TIMESHEET_MODULE = "frappe_slack_connector.api.slash_timesheet"


class TestSlashTimesheet(IntegrationTestCase):
    def test_returns_403_on_signature_verification_failure(self):
        """slash_timesheet returns an HTTP 403 response when verify_slack_request raises."""
        mock_slack = MagicMock()
        mock_slack.verify_slack_request.side_effect = frappe.PermissionError("bad sig")
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.get_data.return_value = ""
        with (
            patch(f"{SLASH_TIMESHEET_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SLASH_TIMESHEET_MODULE}.frappe.request", mock_request),
            patch(f"{SLASH_TIMESHEET_MODULE}.show_timesheet_modal") as mock_show,
        ):
            slash_timesheet()
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_show.assert_not_called()

    def test_delegates_to_show_timesheet_modal_on_valid_request(self):
        """slash_timesheet calls show_timesheet_modal with the SlackIntegration instance and the user/trigger IDs from form_dict."""
        mock_slack = MagicMock()
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.get_data.return_value = ""
        frappe.form_dict = frappe._dict({"user_id": TEST_SLACK_USER_ID, "trigger_id": "T-trigger-9"})
        with (
            patch(f"{SLASH_TIMESHEET_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SLASH_TIMESHEET_MODULE}.frappe.request", mock_request),
            patch(f"{SLASH_TIMESHEET_MODULE}.show_timesheet_modal") as mock_show,
        ):
            slash_timesheet()
        mock_show.assert_called_once_with(mock_slack, TEST_SLACK_USER_ID, "T-trigger-9")

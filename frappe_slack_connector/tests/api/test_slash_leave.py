from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.slash_leave import slash_leave

SLASH_LEAVE_MODULE = "frappe_slack_connector.api.slash_leave"


def _find_block(blocks, block_id):
    """Return the first block whose block_id matches, or None."""
    for block in blocks:
        if block.get("block_id") == block_id:
            return block
    return None


class TestSlashLeave(IntegrationTestCase):
    def test_returns_403_on_signature_verification_failure(self):
        """slash_leave returns an HTTP 403 response when verify_slack_request raises."""
        mock_slack = MagicMock()
        mock_slack.verify_slack_request.side_effect = frappe.PermissionError("bad sig")
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.get_data.return_value = ""
        with (
            patch(f"{SLASH_LEAVE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SLASH_LEAVE_MODULE}.frappe.request", mock_request),
        ):
            slash_leave()
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_slack.slack_app.client.views_open.assert_not_called()

    def test_opens_modal_with_employee_allocated_and_lwp_leave_types(self):
        """slash_leave opens a modal whose leave_type block options include both allocated and LWP leave types."""
        mock_slack = MagicMock()
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.get_data.return_value = ""
        frappe.form_dict = frappe._dict({"user_id": "U001", "trigger_id": "T001"})
        with (
            patch(f"{SLASH_LEAVE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SLASH_LEAVE_MODULE}.frappe.request", mock_request),
            patch(
                f"{SLASH_LEAVE_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SLASH_LEAVE_MODULE}.get_leave_allocation_records",
                return_value={"Casual Leave": {}, "Sick Leave": {}},
            ),
            patch(
                f"{SLASH_LEAVE_MODULE}.frappe.get_all",
                return_value=["LWP"],
            ),
        ):
            slash_leave()
        mock_slack.slack_app.client.views_open.assert_called_once()
        kwargs = mock_slack.slack_app.client.views_open.call_args.kwargs
        view = kwargs["view"]
        self.assertEqual(view["callback_id"], "apply_leave_application")
        leave_type_block = _find_block(view["blocks"], "leave_type")
        leave_type_values = [o["value"] for o in leave_type_block["element"]["options"]]
        self.assertEqual(set(leave_type_values), {"Casual Leave", "Sick Leave", "LWP"})

    def test_opens_error_modal_when_employee_not_found(self):
        """slash_leave opens an error modal when get_employeeid_from_slackid returns None."""
        mock_slack = MagicMock()
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.get_data.return_value = ""
        frappe.form_dict = frappe._dict({"user_id": "U999", "trigger_id": "T999"})
        with (
            patch(f"{SLASH_LEAVE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SLASH_LEAVE_MODULE}.frappe.request", mock_request),
            patch(f"{SLASH_LEAVE_MODULE}.get_employeeid_from_slackid", return_value=None),
        ):
            slash_leave()
        mock_slack.slack_app.client.views_open.assert_called_once()
        kwargs = mock_slack.slack_app.client.views_open.call_args.kwargs
        self.assertEqual(kwargs["view"]["callback_id"], "apply_leave_application_error")

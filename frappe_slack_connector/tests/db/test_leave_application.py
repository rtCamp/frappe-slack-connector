from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.db.leave_application import approve_leave, reject_leave

LEAVE_DB_MODULE = "frappe_slack_connector.db.leave_application"


class TestApproveLeave(IntegrationTestCase):
    def test_applies_workflow_action_when_custom_field_exists(self):
        """approve_leave calls apply_workflow with 'Approve' when custom_first_halfsecond_half exists on Leave Application."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            approve_leave("HR-LAP-0001")
        mock_apply.assert_called_once_with(mock_leave, "Approve")
        mock_leave.save.assert_not_called()

    def test_sets_status_and_saves_and_submits_when_no_custom_field(self):
        """approve_leave sets status='Approved', saves, and submits when the custom field is absent."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            approve_leave("HR-LAP-0002")
        self.assertEqual(mock_leave.status, "Approved")
        mock_leave.save.assert_called_once()
        mock_leave.submit.assert_called_once()
        mock_apply.assert_not_called()

    def test_appends_approved_via_slack_comment(self):
        """approve_leave appends a Comment with text 'approved via Slack' after the approve operation."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
        ):
            approve_leave("HR-LAP-0003")
        mock_leave.add_comment.assert_called_once_with(comment_type="Info", text="approved via Slack")


class TestRejectLeave(IntegrationTestCase):
    def test_applies_workflow_action_when_custom_field_exists(self):
        """reject_leave calls apply_workflow with 'Reject' when custom_first_halfsecond_half exists."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            reject_leave("HR-LAP-0004")
        mock_apply.assert_called_once_with(mock_leave, "Reject")
        mock_leave.save.assert_not_called()

    def test_sets_status_and_saves_and_submits_when_no_custom_field(self):
        """reject_leave sets status='Rejected', saves, and submits when the custom field is absent."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            reject_leave("HR-LAP-0005")
        self.assertEqual(mock_leave.status, "Rejected")
        mock_leave.save.assert_called_once()
        mock_leave.submit.assert_called_once()
        mock_apply.assert_not_called()

    def test_appends_rejected_via_slack_comment(self):
        """reject_leave appends a Comment with text 'rejected via Slack' after the reject operation."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
        ):
            reject_leave("HR-LAP-0006")
        mock_leave.add_comment.assert_called_once_with(comment_type="Info", text="rejected via Slack")

import json
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.slack_interactions import event

MODULE = "frappe_slack_connector.api.slack_interactions"


class TestSlackInteractionsDispatch(IntegrationTestCase):
    def test_ignore_actions_return_before_any_handler(self):
        """A url button (the "Open in ERP" link) still posts a block_actions payload, without a value; it is dropped after signature verification."""
        payload = {
            "type": "block_actions",
            "user": {"id": "U-approver"},
            "actions": [{"type": "button", "action_id": "ignore_open_leave", "block_id": "leave_actions_block"}],
        }
        request = MagicMock()
        request.headers.get.return_value = "x"
        request.get_data.return_value = "payload=..."
        request.form.get.return_value = json.dumps(payload)
        with (
            patch(f"{MODULE}.frappe.request", request),
            patch(f"{MODULE}.SlackIntegration") as mock_slack_cls,
            patch(f"{MODULE}.approve_leave_handler") as mock_approve,
            patch(f"{MODULE}.generate_error_log") as mock_log,
        ):
            result = event()
        mock_slack_cls.return_value.verify_slack_request.assert_called_once()
        mock_approve.assert_not_called()
        mock_log.assert_not_called()
        self.assertIsNone(result)

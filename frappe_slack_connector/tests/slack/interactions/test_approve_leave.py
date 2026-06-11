from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.approve_leave import handler
from frappe_slack_connector.tests import TEST_SLACK_USER_ID, TEST_USER

APPROVE_LEAVE_MODULE = "frappe_slack_connector.slack.interactions.approve_leave"


def _build_action_payload(*, action_id="leave_approve", leave_id="HR-LAP-0001"):
    """Build a Slack block_actions payload mimicking the Approve/Reject button click."""
    return {
        "user": {"id": TEST_SLACK_USER_ID},
        "trigger_id": "T-trigger-1",
        "channel": {"id": "C-channel-1"},
        "container": {"message_ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": leave_id}],
        "message": {
            "blocks": [
                {"block_id": "header_block", "type": "header"},
                {"block_id": "details_block", "type": "section"},
                {"block_id": "leave_actions_block", "type": "actions"},
                {"block_id": "footer_block", "type": "context"},
            ]
        },
    }


class TestApproveLeaveHandler(IntegrationTestCase):
    def test_resolves_slack_approver_and_calls_approve_leave(self):
        """handler resolves the Slack user to a Frappe user via get_userid_from_slackid, sets frappe.session.user, and calls approve_leave."""
        payload = _build_action_payload(action_id="leave_approve", leave_id="HR-LAP-0010")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user") as mock_set_user,
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave") as mock_reject,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_set_user.assert_called_once_with(TEST_USER)
        mock_approve.assert_called_once_with("HR-LAP-0010")
        mock_reject.assert_not_called()

    def test_rejects_leave_when_action_id_is_leave_reject(self):
        """handler calls reject_leave (not approve_leave) when action_id is leave_reject."""
        payload = _build_action_payload(action_id="leave_reject", leave_id="HR-LAP-0011")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave") as mock_reject,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_reject.assert_called_once_with("HR-LAP-0011")
        mock_approve.assert_not_called()

    def test_chat_update_replaces_action_block_with_approved_status(self):
        """After a successful approve, handler calls chat_update with the leave_actions_block replaced by a 'Approved' status section."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave"),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.chat_update.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C-channel-1")
        self.assertEqual(kwargs["ts"], "1700000000.000100")
        # leave_actions_block is now a section, and footer_block is removed.
        block_types = {b.get("type"): b for b in kwargs["blocks"]}
        block_ids = [b.get("block_id") for b in kwargs["blocks"]]
        self.assertIn("section", block_types)
        self.assertIn("Approved", block_types["section"]["text"]["text"])
        self.assertNotIn("footer_block", block_ids)

    def test_chat_update_uses_rejected_status_for_reject_action(self):
        """For a leave_reject action, the chat_update status block reads 'Rejected'."""
        payload = _build_action_payload(action_id="leave_reject")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave"),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        # The replaced block is a fresh section dict with a 'text' key; the original
        # details_block carried block_id but no text. Filter on text presence.
        section_text = next(b["text"]["text"] for b in kwargs["blocks"] if b.get("type") == "section" and "text" in b)
        self.assertIn("Rejected", section_text)

    def test_opens_error_modal_when_db_update_raises(self):
        """handler opens an error modal via views_open when the approve/reject DB call raises."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave", side_effect=RuntimeError("boom")),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.views_open.assert_called_once()
        kwargs = mock_slack.slack_app.client.views_open.call_args.kwargs
        self.assertEqual(kwargs["trigger_id"], "T-trigger-1")
        self.assertEqual(kwargs["view"]["title"]["text"], "Error")
        mock_slack.slack_app.client.chat_update.assert_not_called()

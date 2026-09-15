from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.test_slack_channel import test_channel
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID

TEST_CHANNEL_MODULE = "frappe_slack_connector.api.test_slack_channel"


class TestTestChannel(IntegrationTestCase):
    def test_returns_400_when_channel_id_missing(self):
        """test_channel returns a 400 response when channel_id is None."""
        test_channel(channel_id=None)
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_posts_test_message_to_supplied_channel(self):
        """test_channel calls chat_postMessage with the supplied channel_id."""
        mock_slack = MagicMock()
        with patch(f"{TEST_CHANNEL_MODULE}.SlackIntegration", return_value=mock_slack):
            test_channel(channel_id=TEST_SLACK_CHANNEL_ID)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)

    def test_logs_error_with_msgprint_when_chat_postMessage_raises(self):
        """test_channel routes Slack API errors through generate_error_log with msgprint=True."""
        mock_slack = MagicMock()
        mock_slack.slack_app.client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
        with (
            patch(f"{TEST_CHANNEL_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{TEST_CHANNEL_MODULE}.generate_error_log") as mock_log,
        ):
            test_channel(channel_id=TEST_SLACK_CHANNEL_ID)
        mock_log.assert_called_once()
        self.assertTrue(mock_log.call_args.kwargs["msgprint"])

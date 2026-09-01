from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.auth import connect_slack
from frappe_slack_connector.tests import (
    TEST_SLACK_USER_ID,
    TEST_SLACK_USERNAME,
    TEST_USER,
    make_test_user,
)

AUTH_MODULE = "frappe_slack_connector.api.auth"


class TestConnectSlack(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_test_user(TEST_USER)

    def test_returns_400_when_user_email_not_supplied(self):
        """connect_slack returns a 400 response when user_email is None."""
        connect_slack(user_email=None)
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_upserts_user_meta_with_slack_id_and_username(self):
        """connect_slack writes custom_slack_userid and custom_slack_username onto a User Meta row keyed by the supplied email."""
        mock_slack = MagicMock()
        mock_slack.get_slack_user.return_value = {
            "id": TEST_SLACK_USER_ID,
            "name": TEST_SLACK_USERNAME,
        }
        with patch(f"{AUTH_MODULE}.SlackIntegration", return_value=mock_slack):
            connect_slack(user_email=TEST_USER)
        slack_id = frappe.db.get_value("User Meta", {"user": TEST_USER}, "custom_slack_userid")
        slack_username = frappe.db.get_value("User Meta", {"user": TEST_USER}, "custom_slack_username")
        self.assertEqual(slack_id, TEST_SLACK_USER_ID)
        self.assertEqual(slack_username, TEST_SLACK_USERNAME)

    def test_returns_400_when_slack_user_not_found(self):
        """connect_slack returns a 400 response when get_slack_user returns None."""
        mock_slack = MagicMock()
        mock_slack.get_slack_user.return_value = None
        with patch(f"{AUTH_MODULE}.SlackIntegration", return_value=mock_slack):
            connect_slack(user_email=TEST_USER)
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_shows_msgprint_error_when_slack_user_not_found(self):
        """connect_slack emits a msgprint with the 'Slack user not found' message when get_slack_user returns None."""
        mock_slack = MagicMock()
        mock_slack.get_slack_user.return_value = None
        with (
            patch(f"{AUTH_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{AUTH_MODULE}.frappe.msgprint") as mock_msgprint,
        ):
            connect_slack(user_email=TEST_USER)
        mock_msgprint.assert_called_once()
        msg = mock_msgprint.call_args.kwargs.get("msg") or mock_msgprint.call_args.args[0]
        self.assertIn("Slack user not found", msg)

    def test_returns_500_on_unexpected_exception(self):
        """connect_slack returns 500 and logs the error when SlackIntegration raises an unexpected exception."""
        with (
            patch(
                f"{AUTH_MODULE}.SlackIntegration",
                side_effect=RuntimeError("boom"),
            ),
            patch(f"{AUTH_MODULE}.generate_error_log") as mock_log,
        ):
            connect_slack(user_email=TEST_USER)
        self.assertEqual(frappe.local.response.get("http_status_code"), 500)
        mock_log.assert_called_once()

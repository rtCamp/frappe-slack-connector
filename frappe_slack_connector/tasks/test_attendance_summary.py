# Copyright (c) 2024, rtCamp and Contributors
# See license.txt

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from frappe_slack_connector.tasks.attendance_summary import send_notification


class TestAttendanceSummary(FrappeTestCase):
    """Test the attendance summary batch fetching logic"""

    @patch("frappe_slack_connector.tasks.attendance_summary.SlackIntegration")
    @patch("frappe_slack_connector.tasks.attendance_summary.get_employees_on_leave")
    @patch("frappe.db.get_single_value")
    @patch("frappe.get_all")
    def test_batch_fetch_user_meta(
        self, mock_get_all, mock_get_single_value, mock_get_employees, mock_slack
    ):
        """Test that User Meta is batch fetched instead of per-employee queries"""
        # Setup mocks
        mock_get_single_value.return_value = True  # mention_users = True

        # Mock employees on leave
        mock_get_employees.return_value = [
            {
                "employee": "EMP001",
                "employee_name": "John Doe",
                "from_date": "2024-01-13",
                "to_date": "2024-01-13",
                "half_day": False,
                "half_day_date": None,
            },
            {
                "employee": "EMP002",
                "employee_name": "Jane Smith",
                "from_date": "2024-01-13",
                "to_date": "2024-01-14",
                "half_day": False,
                "half_day_date": None,
            },
        ]

        # Mock frappe.get_all calls
        def get_all_side_effect(doctype, filters=None, fields=None):
            if doctype == "Employee":
                return [
                    SimpleNamespace(name="EMP001", user_id="user1@example.com"),
                    SimpleNamespace(name="EMP002", user_id="user2@example.com"),
                ]
            elif doctype == "User Meta":
                return [
                    SimpleNamespace(user="user1@example.com", custom_slack_userid="U12345"),
                    SimpleNamespace(user="user2@example.com", custom_slack_userid="U67890"),
                ]
            return []

        mock_get_all.side_effect = get_all_side_effect

        # Mock Slack client
        mock_slack_instance = MagicMock()
        mock_slack_instance.SLACK_CHANNEL_ID = "C123456"
        mock_slack_instance.slack_app.client.chat_postMessage.return_value = {"ts": "1234567890.123456"}
        mock_slack.return_value = mock_slack_instance

        # Call the function
        with patch("frappe_slack_connector.tasks.attendance_summary.custom_fields_exist", return_value=False):
            with patch("frappe.utils.nowdate", return_value="2024-01-13"):
                with patch("frappe.utils.getdate", return_value="2024-01-13"):
                    result = send_notification("Employees on Leave")

        # Verify batch fetching was used (only 2 calls to frappe.get_all)
        # 1 for Employee, 1 for User Meta
        self.assertEqual(mock_get_all.call_count, 2)

        # Verify the Employee batch fetch call
        employee_call = mock_get_all.call_args_list[0]
        self.assertEqual(employee_call[0][0], "Employee")
        self.assertIn("name", employee_call[1]["filters"])
        self.assertIn("in", employee_call[1]["filters"]["name"])

        # Verify the User Meta batch fetch call
        user_meta_call = mock_get_all.call_args_list[1]
        self.assertEqual(user_meta_call[0][0], "User Meta")
        self.assertIn("user", user_meta_call[1]["filters"])
        self.assertIn("in", user_meta_call[1]["filters"]["user"])

        # Verify Slack message was posted
        mock_slack_instance.slack_app.client.chat_postMessage.assert_called_once()
        self.assertIsNotNone(result)

    @patch("frappe_slack_connector.tasks.attendance_summary.SlackIntegration")
    @patch("frappe_slack_connector.tasks.attendance_summary.get_employees_on_leave")
    @patch("frappe.db.get_single_value")
    def test_no_employees_on_leave(self, mock_get_single_value, mock_get_employees, mock_slack):
        """Test that empty employee list is handled correctly"""
        # Setup mocks
        mock_get_single_value.return_value = True
        mock_get_employees.return_value = []

        # Mock Slack client
        mock_slack_instance = MagicMock()
        mock_slack_instance.SLACK_CHANNEL_ID = "C123456"
        mock_slack_instance.slack_app.client.chat_postMessage.return_value = {"ts": "1234567890.123456"}
        mock_slack.return_value = mock_slack_instance

        # Call the function
        with patch("frappe_slack_connector.tasks.attendance_summary.custom_fields_exist", return_value=False):
            with patch("frappe.utils.nowdate", return_value="2024-01-13"):
                result = send_notification("Employees on Leave")

        # Verify message was posted with count 0
        mock_slack_instance.slack_app.client.chat_postMessage.assert_called_once()
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()

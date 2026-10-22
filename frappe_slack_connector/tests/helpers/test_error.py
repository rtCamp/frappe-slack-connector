from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.helpers.error import generate_error_log

ERROR_MODULE = "frappe_slack_connector.helpers.error"


class TestGenerateErrorLog(IntegrationTestCase):
    def test_logs_the_traceback_of_the_exception(self):
        """An exception is logged with its traceback, not only str(e): the entry must name the file and line."""
        try:
            raise ValueError("Not permitted")
        except ValueError as e:
            with patch(f"{ERROR_MODULE}.frappe.log_error") as mock_log:
                generate_error_log("Error submitting leave request", exception=e)
        kwargs = mock_log.call_args.kwargs
        self.assertEqual(kwargs["title"], "Slack: Error submitting leave request")
        self.assertIn("ValueError: Not permitted", kwargs["message"])
        self.assertIn("test_error.py", kwargs["message"])
        self.assertIn("Traceback (most recent call last)", kwargs["message"])

    def test_logs_the_message_alone_without_an_exception(self):
        with patch(f"{ERROR_MODULE}.frappe.log_error") as mock_log:
            generate_error_log("Payload not found", message="no payload")
        self.assertEqual(mock_log.call_args.kwargs["message"], "no payload")

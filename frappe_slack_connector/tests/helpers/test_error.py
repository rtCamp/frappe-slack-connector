from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.helpers.error import exception_message, generate_error_log

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


class TestExceptionMessage(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.clear_messages()
        frappe.flags.error_message = None

    def tearDown(self):
        frappe.clear_messages()
        frappe.flags.error_message = None
        super().tearDown()

    def test_queued_messages_are_only_used_for_permission_errors(self):
        """An unrelated earlier msgprint (HRMS "Email sent to ...") must not be shown as the reason for some other bare exception."""
        frappe.msgprint("Email sent to someone@example.com")
        self.assertEqual(exception_message(frappe.ValidationError()), "ValidationError")

    def test_falls_back_to_the_permission_error_flag(self):
        """Frappe's permission checks record the reason in flags.error_message as well."""
        frappe.flags.error_message = "You need the 'write' permission on Leave Application HR-LAP-0001"
        self.assertEqual(
            exception_message(frappe.PermissionError()),
            "You need the 'write' permission on Leave Application HR-LAP-0001",
        )

    def test_keeps_line_breaks_and_joins_list_messages(self):
        """Permission logs are joined with <br>; as_list messages arrive as lists."""
        frappe.msgprint("Not allowed for Employee: X<br>User a@b does not have access")
        self.assertEqual(
            exception_message(frappe.PermissionError()), "Not allowed for Employee: X\nUser a@b does not have access"
        )
        frappe.clear_messages()
        frappe.msgprint(["line a", "line b"], as_list=True)
        self.assertEqual(exception_message(frappe.PermissionError()), "line a\nline b")

    def test_returns_the_exception_text_without_html(self):
        self.assertEqual(exception_message(ValueError("<b>Not</b> permitted")), "Not permitted")

    def test_falls_back_to_the_queued_frappe_message_when_the_exception_has_none(self):
        """A permission failure raises a bare PermissionError and msgprints the reason: show that reason."""
        frappe.msgprint("You are not allowed to access this Leave Application")
        self.assertEqual(
            exception_message(frappe.PermissionError()),
            "You are not allowed to access this Leave Application",
        )

    def test_uses_the_last_queued_message(self):
        frappe.msgprint("older")
        frappe.msgprint("newer")
        self.assertEqual(exception_message(frappe.PermissionError()), "newer")

    def test_names_the_exception_type_when_nothing_else_is_known(self):
        self.assertEqual(exception_message(frappe.PermissionError()), "PermissionError")

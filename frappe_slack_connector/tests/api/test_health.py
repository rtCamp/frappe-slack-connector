from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.health import SLACK_ENTRY_POINTS, slack_endpoints

HEALTH_MODULE = "frappe_slack_connector.api.health"


class TestSlackEndpointsHealth(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.response.pop("http_status_code", None)

    def test_reports_ok_when_every_entry_point_imports(self):
        """With the real modules importable the check answers ok and leaves the status code alone."""
        with patch(f"{HEALTH_MODULE}.frappe.log_error") as mock_log:
            result = slack_endpoints()
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("http_status_code", frappe.response)
        mock_log.assert_not_called()

    def test_reports_503_naming_the_module_that_fails_to_import(self):
        """A broken entry point is named in the response (without the exception text) and logged with it."""
        broken = SLACK_ENTRY_POINTS[0]
        real_import = __import__("importlib").import_module

        def fake_import(name):
            if name == broken:
                raise ImportError("cannot import name 'gone' from 'hrms.hr.utils'")
            return real_import(name)

        with (
            patch(f"{HEALTH_MODULE}.importlib.import_module", side_effect=fake_import),
            patch(f"{HEALTH_MODULE}.frappe.log_error") as mock_log,
        ):
            result = slack_endpoints()
        self.assertEqual(result, {"ok": False, "failed": [broken]})
        self.assertEqual(frappe.response["http_status_code"], 503)
        mock_log.assert_called_once()
        self.assertIn("cannot import name 'gone'", mock_log.call_args.kwargs["message"])
        self.assertNotIn("cannot import name", str(result))

    def test_entry_points_are_the_modules_slack_calls(self):
        self.assertEqual(
            SLACK_ENTRY_POINTS,
            (
                "frappe_slack_connector.api.slack_interactions",
                "frappe_slack_connector.api.slash_leave",
                "frappe_slack_connector.api.slash_timesheet",
            ),
        )

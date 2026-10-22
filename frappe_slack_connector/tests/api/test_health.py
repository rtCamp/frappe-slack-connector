from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.health import LOG_THROTTLE_KEY, SLACK_ENTRY_POINTS, check_entry_points, slack_endpoints

APP = "frappe_slack_connector"
HEALTH_MODULE = f"{APP}.api.health"


class TestSlackEndpointsHealth(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.response.pop("http_status_code", None)
        frappe.cache.delete_value(LOG_THROTTLE_KEY)

    def tearDown(self):
        frappe.response.pop("http_status_code", None)
        frappe.cache.delete_value(LOG_THROTTLE_KEY)
        super().tearDown()

    def test_reports_ok_when_every_entry_point_loads(self):
        """With the real handlers importable the check answers ok and leaves the status code alone."""
        with patch(f"{HEALTH_MODULE}.frappe.log_error") as mock_log:
            result = slack_endpoints()
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("http_status_code", frappe.response)
        mock_log.assert_not_called()

    def test_reports_503_naming_the_entry_point_that_fails_to_import(self):
        """A broken entry point is named in the response (without the exception text) and logged with it."""
        broken = SLACK_ENTRY_POINTS[0]
        real_get_attr = frappe.get_attr

        def fake_get_attr(path):
            if path == broken:
                raise ImportError("cannot import name 'gone' from 'hrms.hr.utils'")
            return real_get_attr(path)

        with (
            patch(f"{HEALTH_MODULE}.frappe.get_attr", side_effect=fake_get_attr),
            patch(f"{HEALTH_MODULE}.frappe.log_error") as mock_log,
        ):
            result = slack_endpoints()
        self.assertEqual(result, {"ok": False, "failed": [broken]})
        self.assertEqual(frappe.response["http_status_code"], 503)
        mock_log.assert_called_once()
        self.assertIn("cannot import name 'gone'", mock_log.call_args.kwargs["message"])
        self.assertNotIn("cannot import name", str(result))

    def test_logs_at_most_once_per_hour_while_broken(self):
        """An anonymous, polled endpoint must not write one Error Log per call."""
        with (
            patch(f"{HEALTH_MODULE}.check_entry_points", return_value={SLACK_ENTRY_POINTS[0]: "ImportError: x"}),
            patch(f"{HEALTH_MODULE}.frappe.log_error") as mock_log,
        ):
            slack_endpoints()
            slack_endpoints()
        mock_log.assert_called_once()
        self.assertEqual(frappe.response["http_status_code"], 503)

    def test_reports_an_entry_point_that_lost_its_guest_whitelist(self):
        """A method that imports but is no longer guest-whitelisted answers 417 to Slack just the same."""
        with patch(f"{HEALTH_MODULE}.frappe.guest_methods", []):
            failures = check_entry_points()
        self.assertEqual(set(failures), set(SLACK_ENTRY_POINTS))
        self.assertTrue(all(error == "not whitelisted for guests" for error in failures.values()))

    def test_entry_points_are_every_guest_method_slack_calls(self):
        """Derived from what the app actually whitelists for guests, so a new Slack endpoint missing from the list fails here."""
        for path in SLACK_ENTRY_POINTS:
            frappe.get_attr(path)
        guest_paths = {
            f"{fn.__module__}.{fn.__name__}" for fn in frappe.guest_methods if fn.__module__.startswith(f"{APP}.")
        }
        self.assertEqual(guest_paths - {f"{HEALTH_MODULE}.slack_endpoints"}, set(SLACK_ENTRY_POINTS))

import importlib
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.health import (
    LOG_THROTTLE_KEY,
    SLACK_ENTRY_POINTS,
    check_entry_points,
    hook_entry_points,
    slack_endpoints,
)
from frappe_slack_connector.tests.test_imports import app_modules

APP = "frappe_slack_connector"
HEALTH_MODULE = f"{APP}.api.health"


class TestSlackEndpointsHealth(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.response.pop("http_status_code", None)
        frappe.cache.delete(frappe.cache.make_key(LOG_THROTTLE_KEY))

    def tearDown(self):
        frappe.response.pop("http_status_code", None)
        frappe.cache.delete(frappe.cache.make_key(LOG_THROTTLE_KEY))
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
        # The claim is a single SET NX EX, so concurrent workers cannot both win it
        ttl = frappe.cache.ttl(frappe.cache.make_key(LOG_THROTTLE_KEY))
        self.assertTrue(0 < ttl <= 3600)
        self.assertEqual(frappe.response["http_status_code"], 503)

    def test_reports_an_entry_point_that_lost_its_guest_whitelist(self):
        """A method that imports but is no longer guest-whitelisted answers 417 to Slack just the same."""
        with patch(f"{HEALTH_MODULE}.frappe.guest_methods", []):
            failures = check_entry_points()
        self.assertEqual(set(failures), set(SLACK_ENTRY_POINTS))
        self.assertTrue(all(error == "not whitelisted for guests" for error in failures.values()))

    def test_entry_points_are_every_guest_method_slack_calls(self):
        """Derived from what the app actually whitelists for guests, so a new Slack endpoint missing from the list fails here.

        frappe.guest_methods only fills when a module is imported, so every app module is imported first;
        otherwise an endpoint in a module nothing has touched yet would go unseen."""
        for name in app_modules():
            importlib.import_module(name)
        guest_paths = {
            f"{fn.__module__}.{fn.__name__}" for fn in frappe.guest_methods if fn.__module__.startswith(f"{APP}.")
        }
        self.assertEqual(guest_paths - {f"{HEALTH_MODULE}.slack_endpoints"}, set(SLACK_ENTRY_POINTS))

    def test_hook_functions_are_checked_too(self):
        """A broken import under a Leave Application hook fails every save, not a Slack request: the check covers it."""
        paths = hook_entry_points()
        self.assertIn(f"{APP}.override.leave_application.after_insert", paths)
        self.assertIn(f"{APP}.tasks.send_daily_reminder.send_reminder", paths)
        broken = f"{APP}.override.leave_application.after_insert"
        real_get_attr = frappe.get_attr

        def fake_get_attr(path):
            if path == broken:
                raise ImportError("cannot import name 'get_leave_balance_on'")
            return real_get_attr(path)

        with patch(f"{HEALTH_MODULE}.frappe.get_attr", side_effect=fake_get_attr):
            failures = check_entry_points()
        self.assertEqual(set(failures), {broken})

    def test_releases_the_hour_and_still_answers_503_when_logging_fails(self):
        """If the Error Log write raises, the monitor still gets the 503 with the list, and the next probe may log again."""
        with (
            patch(f"{HEALTH_MODULE}.check_entry_points", return_value={SLACK_ENTRY_POINTS[0]: "ImportError: x"}),
            patch(f"{HEALTH_MODULE}.frappe.log_error", side_effect=RuntimeError("db down")),
        ):
            result = slack_endpoints()
        self.assertEqual(result, {"ok": False, "failed": [SLACK_ENTRY_POINTS[0]]})
        self.assertEqual(frappe.response["http_status_code"], 503)
        self.assertIsNone(frappe.cache.get(frappe.cache.make_key(LOG_THROTTLE_KEY)))

    def test_health_endpoint_itself_is_guest_get_and_head(self):
        """Dropping allow_guest or the methods would turn the monitor into a 403; pin them."""
        self.assertIn(slack_endpoints, frappe.guest_methods)
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[slack_endpoints], ["GET", "HEAD"])

    def test_reports_an_entry_point_that_no_longer_accepts_post(self):
        """Slack posts; a methods=["GET"] change would give Slack 403 while the method still imports and is guest-whitelisted."""
        method = frappe.get_attr(SLACK_ENTRY_POINTS[1])
        with patch.dict(frappe.allowed_http_methods_for_whitelisted_func, {method: ["GET"]}):
            failures = check_entry_points()
        self.assertEqual(failures, {SLACK_ENTRY_POINTS[1]: "POST not allowed"})

    def test_reports_the_next_pms_helper_when_next_pms_is_installed_but_the_import_failed(self):
        """The workload reminder imports next_pms under try/except; with next_pms present that failure is an upstream rename."""
        with (
            patch(f"{HEALTH_MODULE}.is_next_pms_installed", return_value=True, create=True),
            patch("frappe_slack_connector.tasks.workload_reminder.IMPORT_SUCCESS", False),
            patch("frappe_slack_connector.db.timesheet.is_next_pms_installed", return_value=True),
        ):
            failures = check_entry_points()
        self.assertEqual(set(failures), {f"{APP}.tasks.workload_reminder"})
        with (
            patch("frappe_slack_connector.tasks.workload_reminder.IMPORT_SUCCESS", False),
            patch("frappe_slack_connector.db.timesheet.is_next_pms_installed", return_value=False),
        ):
            self.assertEqual(check_entry_points(), {})

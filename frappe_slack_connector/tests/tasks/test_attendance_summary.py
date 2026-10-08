from datetime import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.tasks.attendance_summary import (
    attendance_channel,
    get_default_holiday_list,
    get_leave_type,
    is_company_holiday,
    send_notification,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID

ATTENDANCE_MODULE = "frappe_slack_connector.tasks.attendance_summary"


def _build_settings_mock(
    *,
    send_attendance_updates=1,
    last_attendance_date=None,
    attendance_time="09:00:00",
    leave_notification_subject="Employees on Leave",
):
    settings = MagicMock()
    settings.send_attendance_updates = send_attendance_updates
    settings.last_attendance_date = last_attendance_date
    settings.attendance_time = attendance_time
    settings.leave_notification_subject = leave_notification_subject
    return settings


class TestAttendanceChannel(IntegrationTestCase):
    def test_returns_silently_when_send_attendance_updates_disabled(self):
        """attendance_channel returns silently when Slack Settings.send_attendance_updates=0."""
        settings = _build_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()
        settings.save.assert_not_called()

    def test_returns_silently_when_today_is_weekend(self):
        """attendance_channel returns silently when today is Saturday or Sunday."""
        # 2026-06-13 is a Saturday.
        settings = _build_settings_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-13"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()

    def test_returns_silently_when_today_is_holiday(self):
        """attendance_channel returns silently when is_company_holiday returns True."""
        settings = _build_settings_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=True),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()

    def test_returns_silently_when_current_time_before_attendance_time(self):
        """attendance_channel returns silently when the current time is before Slack Settings.attendance_time."""
        settings = _build_settings_mock(attendance_time="09:00:00")
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(8, 30))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.get_time", return_value=time(9, 0)),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()

    def test_returns_silently_when_already_run_today(self):
        """attendance_channel returns silently when last_attendance_date equals today (idempotency)."""
        settings = _build_settings_mock(last_attendance_date="2026-06-15")
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.get_time", return_value=time(9, 0)),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()

    def test_persists_attendance_state_when_conditions_met(self):
        """When all guards pass, attendance_channel calls send_notification and writes the returned ts + today's date back onto Slack Settings."""
        settings = _build_settings_mock(last_attendance_date=None)
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_company_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.get_time", return_value=time(9, 0)),
            patch(
                f"{ATTENDANCE_MODULE}.send_notification",
                return_value="1700000000.000999",
            ) as mock_send,
        ):
            attendance_channel()
        mock_send.assert_called_once()
        self.assertEqual(settings.last_attendance_date, "2026-06-15")
        self.assertEqual(settings.last_attendance_msg_ts, "1700000000.000999")
        settings.save.assert_called_once_with(ignore_permissions=True)


class TestSendNotification(IntegrationTestCase):
    def test_posts_attendance_blocks_to_slack_channel_and_returns_ts(self):
        """send_notification posts chat.postMessage to SLACK_CHANNEL_ID and returns the message ts from Slack's response."""
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.slack_app.client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1700000000.000123",
        }
        with (
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{ATTENDANCE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.get_employees_on_leave", return_value=[]),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
        ):
            result = send_notification("Employees on Leave")
        self.assertEqual(result, "1700000000.000123")
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)

    def test_logs_error_and_returns_none_when_slack_post_raises(self):
        """send_notification logs an error via generate_error_log when chat_postMessage raises, and returns None."""
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.slack_app.client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
        with (
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{ATTENDANCE_MODULE}.frappe.db.get_single_value", return_value=0),
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.get_employees_on_leave", return_value=[]),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(f"{ATTENDANCE_MODULE}.generate_error_log") as mock_log,
        ):
            result = send_notification("Employees on Leave")
        self.assertIsNone(result)
        mock_log.assert_called_once()


class TestGetLeaveType(IntegrationTestCase):
    def test_returns_full_day_when_not_half_day(self):
        """get_leave_type returns 'Full Day' when the leave is not a half day."""
        application = frappe._dict({"half_day": 0, "half_day_date": None})
        with patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"):
            result = get_leave_type(application)
        self.assertEqual(result, "Full Day")

    def test_returns_full_day_when_half_day_date_is_not_today(self):
        """get_leave_type returns 'Full Day' when half_day is set but half_day_date is not today."""
        from frappe.utils import getdate

        application = frappe._dict({"half_day": 1, "half_day_date": getdate("2026-06-20")})
        with patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"):
            result = get_leave_type(application)
        self.assertEqual(result, "Full Day")

    def test_returns_half_day_when_custom_fields_absent(self):
        """get_leave_type returns 'Half Day' when half_day_date matches today and the rtCamp custom field is absent."""
        from frappe.utils import getdate

        application = frappe._dict({"half_day": 1, "half_day_date": getdate("2026-06-15")})
        with (
            patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
        ):
            result = get_leave_type(application)
        self.assertEqual(result, "Half Day")

    def test_returns_first_half_when_custom_field_indicates_first(self):
        """get_leave_type returns 'First-Half' when custom_first_halfsecond_half == 'First Half'."""
        from frappe.utils import getdate

        application = frappe._dict(
            {
                "half_day": 1,
                "half_day_date": getdate("2026-06-15"),
                "custom_first_halfsecond_half": "First Half",
            }
        )
        with (
            patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=True),
        ):
            result = get_leave_type(application)
        self.assertEqual(result, "First-Half")

    def test_returns_second_half_when_custom_field_indicates_second(self):
        """get_leave_type returns 'Second-Half' when custom_first_halfsecond_half == 'Second Half'."""
        from frappe.utils import getdate

        application = frappe._dict(
            {
                "half_day": 1,
                "half_day_date": getdate("2026-06-15"),
                "custom_first_halfsecond_half": "Second Half",
            }
        )
        with (
            patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=True),
        ):
            result = get_leave_type(application)
        self.assertEqual(result, "Second-Half")


class TestIsCompanyHoliday(IntegrationTestCase):
    def test_checks_the_date_against_the_default_company_holiday_list(self):
        """is_company_holiday resolves the default company's holiday list and passes it, with the date, to erpnext's is_holiday."""
        with (
            patch(f"{ATTENDANCE_MODULE}.get_default_holiday_list", return_value="Company Holidays 2026"),
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=True) as mock_is_holiday,
        ):
            self.assertTrue(is_company_holiday("2026-06-15"))
        mock_is_holiday.assert_called_once_with("Company Holidays 2026", "2026-06-15")

    def test_is_not_a_holiday_without_a_holiday_list(self):
        """is_company_holiday returns False, without querying, when no holiday list is configured."""
        with (
            patch(f"{ATTENDANCE_MODULE}.get_default_holiday_list", return_value=None),
            patch(f"{ATTENDANCE_MODULE}.is_holiday") as mock_is_holiday,
        ):
            self.assertFalse(is_company_holiday("2026-06-15"))
        mock_is_holiday.assert_not_called()


class TestGetDefaultHolidayList(IntegrationTestCase):
    def test_returns_the_default_company_holiday_list(self):
        """get_default_holiday_list reads Company.default_holiday_list of the global default company."""
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.defaults.get_global_default", return_value="Acme"),
            patch(f"{ATTENDANCE_MODULE}.frappe.get_cached_value", return_value="Acme Holidays") as mock_cached,
        ):
            self.assertEqual(get_default_holiday_list(), "Acme Holidays")
        mock_cached.assert_called_once_with("Company", "Acme", "default_holiday_list")

    def test_returns_none_without_a_default_company(self):
        """get_default_holiday_list returns None, without a Company lookup, when no default company is set."""
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.defaults.get_global_default", return_value=None),
            patch(f"{ATTENDANCE_MODULE}.frappe.get_cached_value") as mock_cached,
        ):
            self.assertIsNone(get_default_holiday_list())
        mock_cached.assert_not_called()

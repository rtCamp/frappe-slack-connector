import contextlib
from datetime import datetime, time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from frappe_slack_connector.tasks.attendance_summary import (
    attendance_channel,
    build_attendance_blocks,
    get_leave_type,
    send_notification,
    update_attendance_summary,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID

ATTENDANCE_MODULE = "frappe_slack_connector.tasks.attendance_summary"


def _build_settings_mock(
    *,
    send_attendance_updates=1,
    last_attendance_date=None,
    last_attendance_msg_ts=None,
    attendance_time="09:00:00",
    leave_notification_subject="Employees on Leave",
):
    settings = MagicMock()
    settings.send_attendance_updates = send_attendance_updates
    settings.last_attendance_date = last_attendance_date
    settings.last_attendance_msg_ts = last_attendance_msg_ts
    settings.attendance_time = attendance_time
    settings.leave_notification_subject = leave_notification_subject
    return settings


def _build_leave_row(
    *,
    employee="EMP-001",
    employee_name="Alice",
    from_date="2026-06-15",
    to_date="2026-06-15",
    half_day=0,
    half_day_date=None,
):
    """Build a frappe._dict row shaped like a get_employees_on_leave result, with real date objects."""
    return frappe._dict(
        {
            "employee": employee,
            "employee_name": employee_name,
            "leave_type": "Casual Leave",
            "from_date": getdate(from_date),
            "to_date": getdate(to_date),
            "status": "Approved",
            "half_day": half_day,
            "half_day_date": getdate(half_day_date) if half_day_date else None,
        }
    )


@contextlib.contextmanager
def _patch_block_inputs(users_on_leave):
    """Patch everything build_attendance_blocks reads so it runs without Slack or DB rows, on 2026-06-15."""
    with (
        patch(f"{ATTENDANCE_MODULE}.frappe.db.get_single_value", return_value=0),
        patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
        patch(f"{ATTENDANCE_MODULE}.get_employees_on_leave", return_value=users_on_leave),
        patch(f"{ATTENDANCE_MODULE}.frappe.get_all", return_value=[]),
        patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
        patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
    ):
        yield


UPDATED_AT_BLOCK = {
    "type": "context",
    "elements": [{"type": "mrkdwn", "text": "_Updated at 14:32_"}],
}


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
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=False),
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
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=False),
            patch(f"{ATTENDANCE_MODULE}.send_notification") as mock_send,
        ):
            attendance_channel()
        mock_send.assert_not_called()

    def test_returns_silently_when_today_is_holiday(self):
        """attendance_channel returns silently when is_holiday returns True."""
        settings = _build_settings_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=True),
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
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=False),
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
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=False),
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
            patch(f"{ATTENDANCE_MODULE}.is_holiday", return_value=False),
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


class TestBuildAttendanceBlocks(IntegrationTestCase):
    def test_morning_and_update_blocks_differ_only_by_updated_at_context(self):
        """For the same leave data, build_attendance_blocks with updated_at returns the morning blocks plus one trailing 'Updated at' context block and nothing else."""
        rows = [
            _build_leave_row(employee="EMP-001", employee_name="Alice", to_date="2026-06-17"),
            _build_leave_row(employee="EMP-002", employee_name="Bob"),
        ]
        with _patch_block_inputs(rows):
            morning = build_attendance_blocks("Employees on Leave")
            updated = build_attendance_blocks("Employees on Leave", updated_at="14:32")
        self.assertEqual(updated, [*morning, UPDATED_AT_BLOCK])

    def test_morning_blocks_have_no_context_block(self):
        """Without updated_at, build_attendance_blocks returns only the header and section blocks (no context block)."""
        rows = [_build_leave_row()]
        with _patch_block_inputs(rows):
            morning = build_attendance_blocks("Employees on Leave")
        self.assertEqual([b["type"] for b in morning], ["header", "section"])
        self.assertIn("1 Employees on Leave", morning[0]["text"]["text"])

    def test_no_one_on_leave_update_appends_context_to_header_only_variant(self):
        """When nobody is on leave, the update variant is the single 'No ...' header followed by the 'Updated at' context block."""
        with _patch_block_inputs([]):
            morning = build_attendance_blocks("Employees on Leave")
            updated = build_attendance_blocks("Employees on Leave", updated_at="14:32")
        self.assertEqual([b["type"] for b in morning], ["header"])
        self.assertIn("No Employees on Leave", morning[0]["text"]["text"])
        self.assertEqual(updated, [*morning, UPDATED_AT_BLOCK])


class TestUpdateAttendanceSummary(IntegrationTestCase):
    def test_calls_chat_update_with_stored_ts_and_updated_blocks(self):
        """update_attendance_summary rebuilds the blocks with the current time and calls chat_update on SLACK_CHANNEL_ID with the stored last_attendance_msg_ts."""
        settings = _build_settings_mock(
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
        )
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs([]),
        ):
            update_attendance_summary()
        mock_slack.slack_app.client.chat_update.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertEqual(kwargs["ts"], "1700000000.000777")
        self.assertEqual(kwargs["blocks"][-1], UPDATED_AT_BLOCK)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_uses_leave_notification_subject_as_title(self):
        """update_attendance_summary builds the blocks with Slack Settings.leave_notification_subject as the title, like the morning post."""
        settings = _build_settings_mock(
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
            leave_notification_subject="Team Members on Leave",
        )
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs([]),
        ):
            update_attendance_summary()
        header_text = mock_slack.slack_app.client.chat_update.call_args.kwargs["blocks"][0]["text"]["text"]
        self.assertIn("Team Members on Leave", header_text)

    def test_does_nothing_when_summary_not_posted_today(self):
        """update_attendance_summary returns without touching Slack when last_attendance_msg_ts is missing or last_attendance_date is not today."""
        mock_slack = MagicMock()
        for settings in (
            _build_settings_mock(last_attendance_date="2026-06-15", last_attendance_msg_ts=None),
            _build_settings_mock(last_attendance_date="2026-06-14", last_attendance_msg_ts="1700000000.000777"),
            _build_settings_mock(last_attendance_date=None, last_attendance_msg_ts="1700000000.000777"),
        ):
            with (
                patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
                patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
                patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
                patch(f"{ATTENDANCE_MODULE}.build_attendance_blocks") as mock_build,
            ):
                update_attendance_summary()
            mock_build.assert_not_called()
        mock_slack.slack_app.client.chat_update.assert_not_called()

    def test_logs_error_and_does_not_raise_when_chat_update_raises(self):
        """When chat_update raises, update_attendance_summary logs via generate_error_log and swallows the exception."""
        settings = _build_settings_mock(
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
        )
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.slack_app.client.chat_update.side_effect = RuntimeError("message_not_found")
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            patch(f"{ATTENDANCE_MODULE}.generate_error_log") as mock_log,
            _patch_block_inputs([]),
        ):
            update_attendance_summary()
        mock_log.assert_called_once()
        self.assertIsInstance(mock_log.call_args.kwargs["exception"], RuntimeError)


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

import contextlib
from datetime import datetime, time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from frappe_slack_connector.tasks.attendance_summary import (
    ATTENDANCE_UPDATE_LOCK,
    attendance_blocks_hash,
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
    last_attendance_blocks_hash=None,
    attendance_time="09:00:00",
    leave_notification_subject="Employees on Leave",
):
    settings = MagicMock()
    settings.send_attendance_updates = send_attendance_updates
    settings.last_attendance_date = last_attendance_date
    settings.last_attendance_msg_ts = last_attendance_msg_ts
    settings.last_attendance_blocks_hash = last_attendance_blocks_hash
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
    """Patch everything build_attendance_blocks reads so it runs without Slack or DB rows, on 2026-06-15.

    Also stubs frappe.db.set_single_value (the content-hash and day stamps) and yields that mock.
    """
    with (
        patch(f"{ATTENDANCE_MODULE}.frappe.db.get_single_value", return_value=0),
        patch(f"{ATTENDANCE_MODULE}.frappe.db.set_single_value") as mock_set_single_value,
        patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
        patch(f"{ATTENDANCE_MODULE}.get_employees_on_leave", return_value=users_on_leave),
        patch(f"{ATTENDANCE_MODULE}.frappe.get_all", return_value=[]),
        patch(f"{ATTENDANCE_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
        patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
    ):
        yield mock_set_single_value


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
            patch(f"{ATTENDANCE_MODULE}.frappe.db.set_single_value") as mock_set_single_value,
        ):
            attendance_channel()
        mock_send.assert_not_called()
        mock_set_single_value.assert_not_called()

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
            patch(f"{ATTENDANCE_MODULE}.frappe.db.set_single_value") as mock_set_single_value,
        ):
            attendance_channel()
        mock_send.assert_called_once()
        # A direct write, not save(): the stale in-memory doc must not overwrite
        # the content hash the post just stored
        mock_set_single_value.assert_called_once_with(
            "Slack Settings",
            {"last_attendance_date": "2026-06-15", "last_attendance_msg_ts": "1700000000.000999"},
        )
        settings.save.assert_not_called()


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
            patch(f"{ATTENDANCE_MODULE}.frappe.db.set_single_value") as mock_set_single_value,
        ):
            result = send_notification("Employees on Leave")
        self.assertEqual(result, "1700000000.000123")
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        # The fingerprint of what was posted is stored for the in-place update to compare against
        mock_set_single_value.assert_called_once_with(
            "Slack Settings", "last_attendance_blocks_hash", attendance_blocks_hash(kwargs["blocks"])
        )

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
            patch(f"{ATTENDANCE_MODULE}.frappe.db.set_single_value") as mock_set_single_value,
        ):
            result = send_notification("Employees on Leave")
        self.assertIsNone(result)
        mock_log.assert_called_once()
        mock_set_single_value.assert_not_called()


def _post_and_update(rows, *, leave_notification_subject="Employees on Leave"):
    """Run the morning post (send_notification) and then the in-place update (update_attendance_summary) against the same leave rows and Slack mock; return the mock Slack client."""
    settings = _build_settings_mock(
        last_attendance_date="2026-06-15",
        last_attendance_msg_ts="1700000000.000777",
        leave_notification_subject=leave_notification_subject,
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
        _patch_block_inputs(rows),
    ):
        send_notification(leave_notification_subject)
        update_attendance_summary()
    return mock_slack.slack_app.client


class TestBuildAttendanceBlocks(IntegrationTestCase):
    def test_morning_post_and_in_place_update_differ_only_by_updated_at_context(self):
        """For the same leave data, the blocks sent by update_attendance_summary (chat_update) equal the blocks posted by send_notification (chat_postMessage) plus one trailing 'Updated at' context block."""
        rows = [
            _build_leave_row(employee="EMP-001", employee_name="Alice", to_date="2026-06-17"),
            _build_leave_row(employee="EMP-002", employee_name="Bob"),
        ]
        client = _post_and_update(rows)
        posted = client.chat_postMessage.call_args.kwargs["blocks"]
        updated = client.chat_update.call_args.kwargs["blocks"]
        self.assertEqual([b["type"] for b in posted], ["header", "section"])
        self.assertIn("2 Employees on Leave", posted[0]["text"]["text"])
        self.assertEqual(updated, [*posted, UPDATED_AT_BLOCK])

    def test_morning_blocks_have_no_context_block(self):
        """Without updated_at, build_attendance_blocks returns only the header and section blocks (no context block)."""
        rows = [_build_leave_row()]
        with _patch_block_inputs(rows):
            morning = build_attendance_blocks("Employees on Leave")
        self.assertEqual([b["type"] for b in morning], ["header", "section"])
        self.assertIn("1 Employees on Leave", morning[0]["text"]["text"])

    def test_no_one_on_leave_update_appends_context_to_header_only_variant(self):
        """When nobody is on leave, the morning post is the single 'No ...' header and the in-place update is that header followed by the 'Updated at' context block."""
        client = _post_and_update([])
        posted = client.chat_postMessage.call_args.kwargs["blocks"]
        updated = client.chat_update.call_args.kwargs["blocks"]
        self.assertEqual([b["type"] for b in posted], ["header"])
        self.assertIn("No Employees on Leave", posted[0]["text"]["text"])
        self.assertEqual(updated, [*posted, UPDATED_AT_BLOCK])

    def test_build_attendance_blocks_without_updated_at_has_no_context_block(self):
        """build_attendance_blocks only appends the context block when updated_at is given."""
        with _patch_block_inputs([_build_leave_row()]):
            morning = build_attendance_blocks("Employees on Leave")
            updated = build_attendance_blocks("Employees on Leave", updated_at="14:32")
        self.assertNotIn("context", [b["type"] for b in morning])
        self.assertEqual(updated[-1], UPDATED_AT_BLOCK)


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

    def test_runs_whole_job_under_site_filelock(self):
        """update_attendance_summary enters the fsc_attendance_summary_update file lock before reading settings, so concurrent refreshes are serialised and cannot overwrite a newer edit with an older read."""
        settings = _build_settings_mock(
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
        )
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        order = []
        mock_lock = MagicMock()
        mock_lock.return_value.__enter__.side_effect = lambda *a: order.append("lock")
        # A MagicMock __exit__ is truthy and would swallow exceptions raised in the block
        mock_lock.return_value.__exit__.return_value = False
        with (
            patch(f"{ATTENDANCE_MODULE}.filelock", mock_lock),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.get_single",
                side_effect=lambda *a, **k: (order.append("settings"), settings)[1],
            ),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs([]),
        ):
            update_attendance_summary()
        mock_lock.assert_called_once_with(ATTENDANCE_UPDATE_LOCK, timeout=60)
        self.assertEqual(ATTENDANCE_UPDATE_LOCK, "fsc_attendance_summary_update")
        self.assertEqual(order, ["lock", "settings"])
        mock_lock.return_value.__exit__.assert_called_once()
        mock_slack.slack_app.client.chat_update.assert_called_once()

    def test_does_nothing_when_attendance_updates_disabled(self):
        """update_attendance_summary re-checks send_attendance_updates and returns without building or editing when it is 0 (e.g. switched off between enqueue and run)."""
        settings = _build_settings_mock(
            send_attendance_updates=0,
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
        )
        mock_slack = MagicMock()
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{ATTENDANCE_MODULE}.today", return_value="2026-06-15"),
            patch(f"{ATTENDANCE_MODULE}.build_attendance_blocks") as mock_build,
        ):
            update_attendance_summary()
        mock_build.assert_not_called()
        mock_slack.slack_app.client.chat_update.assert_not_called()

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


class TestAttendanceBlocksHash(IntegrationTestCase):
    def test_ignores_the_updated_at_context_block(self):
        """The fingerprint covers the summary content only: appending or changing the "Updated at" block does not change it."""
        with _patch_block_inputs([_build_leave_row()]):
            morning = build_attendance_blocks("Employees on Leave")
            edited = build_attendance_blocks("Employees on Leave", updated_at="14:32")
            edited_later = build_attendance_blocks("Employees on Leave", updated_at="16:05")
        self.assertEqual(attendance_blocks_hash(morning), attendance_blocks_hash(edited))
        self.assertEqual(attendance_blocks_hash(edited), attendance_blocks_hash(edited_later))

    def test_changes_when_the_people_on_leave_change(self):
        """A different set of people on leave produces a different fingerprint."""
        with _patch_block_inputs([_build_leave_row(employee="EMP-001", employee_name="Alice")]):
            one = build_attendance_blocks("Employees on Leave")
        with _patch_block_inputs([_build_leave_row(employee="EMP-002", employee_name="Bob")]):
            other = build_attendance_blocks("Employees on Leave")
        with _patch_block_inputs([]):
            nobody = build_attendance_blocks("Employees on Leave")
        self.assertNotEqual(attendance_blocks_hash(one), attendance_blocks_hash(other))
        self.assertNotEqual(attendance_blocks_hash(one), attendance_blocks_hash(nobody))

    def test_is_stable_across_calls(self):
        """The same content hashes to the same value, so a stored fingerprint can be compared later."""
        with _patch_block_inputs([_build_leave_row()]):
            first = attendance_blocks_hash(build_attendance_blocks("Employees on Leave"))
            second = attendance_blocks_hash(build_attendance_blocks("Employees on Leave"))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)


class TestUpdateAttendanceSummarySkipsUnchangedContent(IntegrationTestCase):
    def _settings(self, stored_hash):
        return _build_settings_mock(
            last_attendance_date="2026-06-15",
            last_attendance_msg_ts="1700000000.000777",
            last_attendance_blocks_hash=stored_hash,
        )

    def test_skips_slack_edit_when_content_matches_stored_hash(self):
        """When the rebuilt content hashes to the stored fingerprint (an earlier job of the same burst already posted it) no chat_update is made and nothing is stored."""
        rows = [_build_leave_row()]
        with _patch_block_inputs(rows):
            stored = attendance_blocks_hash(build_attendance_blocks("Employees on Leave"))
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=self._settings(stored)),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs(rows) as mock_set_single_value,
        ):
            update_attendance_summary()
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_set_single_value.assert_not_called()

    def test_edits_and_stores_hash_when_content_differs(self):
        """When the content differs from the stored fingerprint, chat_update is made and the new fingerprint (of the content, without the Updated at block) is stored afterwards."""
        rows = [_build_leave_row()]
        with _patch_block_inputs([]):
            stale = attendance_blocks_hash(build_attendance_blocks("Employees on Leave"))
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=self._settings(stale)),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs(rows) as mock_set_single_value,
        ):
            update_attendance_summary()
        mock_slack.slack_app.client.chat_update.assert_called_once()
        sent_blocks = mock_slack.slack_app.client.chat_update.call_args.kwargs["blocks"]
        self.assertEqual(sent_blocks[-1], UPDATED_AT_BLOCK)
        mock_set_single_value.assert_called_once_with(
            "Slack Settings", "last_attendance_blocks_hash", attendance_blocks_hash(sent_blocks)
        )

    def test_edits_when_no_hash_is_stored_yet(self):
        """A summary posted before the fingerprint existed (empty stored hash) is always edited."""
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=self._settings(None)),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs([]) as mock_set_single_value,
        ):
            update_attendance_summary()
        mock_slack.slack_app.client.chat_update.assert_called_once()
        mock_set_single_value.assert_called_once()

    def test_does_not_store_hash_when_chat_update_fails(self):
        """If Slack rejects the edit the stored fingerprint is left alone, so the next refresh retries the edit instead of believing it went through."""
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.slack_app.client.chat_update.side_effect = RuntimeError("ratelimited")
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=self._settings("stale")),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            patch(f"{ATTENDANCE_MODULE}.generate_error_log") as mock_log,
            _patch_block_inputs([]) as mock_set_single_value,
        ):
            update_attendance_summary()
        mock_log.assert_called_once()
        mock_set_single_value.assert_not_called()

    def test_burst_of_refreshes_edits_slack_once(self):
        """Three refresh jobs for the same committed state (a bulk reject) make exactly one chat_update: the stored fingerprint advances after the first and the rest skip."""
        settings = self._settings(None)

        def remember_hash(doctype, fieldname, value=None, **kwargs):
            if fieldname == "last_attendance_blocks_hash":
                settings.last_attendance_blocks_hash = value

        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        with (
            patch(f"{ATTENDANCE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{ATTENDANCE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(
                f"{ATTENDANCE_MODULE}.frappe.utils.now_datetime",
                return_value=datetime(2026, 6, 15, 14, 32),
            ),
            _patch_block_inputs([_build_leave_row()]) as mock_set_single_value,
        ):
            mock_set_single_value.side_effect = remember_hash
            for _ in range(3):
                update_attendance_summary()
        mock_slack.slack_app.client.chat_update.assert_called_once()


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

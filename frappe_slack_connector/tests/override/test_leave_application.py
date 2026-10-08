from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.override.leave_application import (
    after_insert,
    on_update_refresh_attendance_summary,
    send_leave_notification_bg,
    send_leave_notification_to_applicant,
    update_attendance_summary,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, TEST_SLACK_USER_ID

LEAVE_OVERRIDE_MODULE = "frappe_slack_connector.override.leave_application"


def _build_leave_doc(
    *,
    name="HR-LAP-0050",
    employee="EMP-001",
    employee_name="Alice",
    leave_approver="approver@x.com",
    leave_type="Casual Leave",
    from_date="2026-06-10",
    to_date="2026-06-12",
    description="vacation",
    half_day=0,
    half_day_date=None,
    creation="2026-06-09 09:00:00",
    status="Open",
    status_changed=True,
):
    """Build a MagicMock that mimics a Leave Application doc with the fields the override code reads.

    status_changed drives doc.has_value_changed("status"), which the refresh handler gates on.
    """
    doc = MagicMock()
    doc.name = name
    doc.status = status
    doc.has_value_changed.side_effect = lambda fieldname: status_changed if fieldname == "status" else False
    doc.employee = employee
    doc.employee_name = employee_name
    doc.leave_approver = leave_approver
    doc.leave_type = leave_type
    doc.from_date = from_date
    doc.to_date = to_date
    doc.description = description
    doc.half_day = half_day
    doc.half_day_date = half_day_date
    doc.creation = creation
    return doc


def _build_slack_settings_mock(
    *,
    send_attendance_updates=1,
    last_attendance_date="2026-06-10",
    last_attendance_msg_ts="1700000000.000001",
):
    """Build a MagicMock that mimics Slack Settings Single doc."""
    settings = MagicMock()
    settings.send_attendance_updates = send_attendance_updates
    settings.last_attendance_date = last_attendance_date
    settings.last_attendance_msg_ts = last_attendance_msg_ts
    return settings


def _refresh_calls(mock_enqueue):
    """Return the enqueue calls that target update_attendance_summary."""
    return [call for call in mock_enqueue.call_args_list if call.args[0] is update_attendance_summary]


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg and send_leave_notification_to_applicant, both on the short queue."""
        doc = _build_leave_doc()
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
        ):
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        targets = [call.args[0] for call in mock_enqueue.call_args_list]
        self.assertIn(send_leave_notification_bg, targets)
        self.assertIn(send_leave_notification_to_applicant, targets)
        for call in mock_enqueue.call_args_list:
            self.assertEqual(call.kwargs["queue"], "short")
            self.assertIs(call.kwargs["doc"], doc)

    def test_enqueues_summary_refresh_when_new_leave_covers_today_and_summary_posted(self):
        """after_insert also enqueues update_attendance_summary when the new leave covers today and today's summary has already been posted."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-12")
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 3)
        refresh = _refresh_calls(mock_enqueue)
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0].kwargs["queue"], "short")
        self.assertTrue(refresh[0].kwargs["enqueue_after_commit"])

    def test_does_not_enqueue_summary_refresh_when_new_leave_does_not_cover_today(self):
        """after_insert does not enqueue update_attendance_summary when the new leave starts after today, even if today's summary is posted."""
        doc = _build_leave_doc(from_date="2026-06-11", to_date="2026-06-12")
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        self.assertEqual(_refresh_calls(mock_enqueue), [])


class TestOnUpdateRefreshAttendanceSummary(IntegrationTestCase):
    def _run(self, doc, settings, today="2026-06-10"):
        """Call the handler with Slack Settings and today's date patched; return the enqueue mock."""
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value=today),
        ):
            on_update_refresh_attendance_summary(doc, method="on_update")
        return mock_enqueue

    def test_enqueues_refresh_when_leave_covering_today_is_rejected_after_summary(self):
        """Rejecting a leave that covers today, after today's summary is posted, enqueues update_attendance_summary on the short queue after commit."""
        doc = _build_leave_doc(from_date="2026-06-09", to_date="2026-06-11", status="Rejected")
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_called_once()
        call = mock_enqueue.call_args
        self.assertIs(call.args[0], update_attendance_summary)
        self.assertEqual(call.kwargs["queue"], "short")
        self.assertTrue(call.kwargs["enqueue_after_commit"])

    def test_enqueues_refresh_when_leave_covering_today_is_cancelled(self):
        """Cancelling a leave that covers today (status becomes Cancelled, as HRMS before_cancel sets it) also enqueues the refresh."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Cancelled")
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], update_attendance_summary)

    def test_does_nothing_when_summary_not_posted_yet(self):
        """Rejecting a leave before today's summary is posted (no ts, or last_attendance_date is a previous day) enqueues nothing; the morning post will exclude it on its own."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected")
        for settings in (
            _build_slack_settings_mock(last_attendance_date="2026-06-10", last_attendance_msg_ts=None),
            _build_slack_settings_mock(last_attendance_date="2026-06-09"),
            _build_slack_settings_mock(last_attendance_date=None),
        ):
            mock_enqueue = self._run(doc, settings)
            mock_enqueue.assert_not_called()

    def test_does_nothing_when_attendance_updates_disabled(self):
        """The refresh is not enqueued when Slack Settings.send_attendance_updates=0."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected")
        settings = _build_slack_settings_mock(send_attendance_updates=0, last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_not_called()

    def test_does_nothing_when_leave_does_not_cover_today(self):
        """Rejecting a leave whose from_date..to_date range does not include today enqueues nothing."""
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        for from_date, to_date in (("2026-06-11", "2026-06-12"), ("2026-06-08", "2026-06-09")):
            doc = _build_leave_doc(from_date=from_date, to_date=to_date, status="Rejected")
            mock_enqueue = self._run(doc, settings)
            mock_enqueue.assert_not_called()

    def test_does_nothing_when_status_unchanged(self):
        """Saving an already-rejected leave without changing its status does not rebuild the summary."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected", status_changed=False)
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings) as mock_get_single,
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            on_update_refresh_attendance_summary(doc, method="on_update")
        mock_enqueue.assert_not_called()
        mock_get_single.assert_not_called()

    def test_does_nothing_when_status_changes_to_approved(self):
        """A status change to Approved (or Open) is not a removal and does not enqueue the refresh."""
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        for status in ("Approved", "Open"):
            doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status=status)
            mock_enqueue = self._run(doc, settings)
            mock_enqueue.assert_not_called()

    def test_handles_date_objects_on_doc(self):
        """from_date/to_date may be date objects rather than strings; the today-in-range check still works."""
        from frappe.utils import getdate

        doc = _build_leave_doc(from_date=getdate("2026-06-10"), to_date=getdate("2026-06-10"), status="Rejected")
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_called_once()


class TestSendLeaveNotificationBg(IntegrationTestCase):
    def test_posts_chat_message_to_approver_dm_with_application_blocks(self):
        """send_leave_notification_bg calls chat_postMessage with the approver's Slack DM channel when the approver has a Slack ID."""
        doc = _build_leave_doc(from_date="2026-06-15")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.get_slack_user_id.side_effect = ["U-approver", "U-applicant"]
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today",
                return_value="2026-06-10",
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate",
                return_value="2026-06-10",
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=False),
        ):
            send_leave_notification_bg(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], "U-approver")

    def test_posts_thread_reply_to_attendance_channel_when_leave_covers_today(self):
        """When from_date == today, send_attendance_updates=1, and last_attendance_msg_ts is set, also posts a thread reply to the attendance channel."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.get_slack_user_id.side_effect = ["U-approver", "U-applicant"]
        settings = _build_slack_settings_mock(send_attendance_updates=1, last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today",
                return_value="2026-06-10",
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate",
                return_value="2026-06-10",
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=False),
        ):
            send_leave_notification_bg(doc)
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_count, 2)
        attendance_call = next(
            c
            for c in mock_slack.slack_app.client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1700000000.000001"
        )
        self.assertEqual(attendance_call.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertTrue(attendance_call.kwargs["reply_broadcast"])

    def test_does_not_post_thread_reply_when_attendance_updates_disabled(self):
        """The attendance-channel thread reply does not fire when send_attendance_updates=0."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
        mock_slack.get_slack_user_id.side_effect = ["U-approver", "U-applicant"]
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today",
                return_value="2026-06-10",
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate",
                return_value="2026-06-10",
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=False),
        ):
            send_leave_notification_bg(doc)
        for call in mock_slack.slack_app.client.chat_postMessage.call_args_list:
            self.assertNotIn("thread_ts", call.kwargs)

    def test_skips_approver_dm_when_approver_has_no_slack_id(self):
        """When the approver's Slack ID cannot be resolved, the approver DM is not posted (silently). Other side effects still run."""
        doc = _build_leave_doc(from_date="2026-06-15")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID

        # First call (for approver) raises; second call (for applicant) returns a Slack id.
        def side_effect(*args, **kwargs):
            if kwargs.get("user_email") == "approver@x.com":
                raise RuntimeError("no meta")
            return "U-applicant"

        mock_slack.get_slack_user_id.side_effect = side_effect
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today",
                return_value="2026-06-10",
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate",
                return_value="2026-06-10",
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log"),
        ):
            send_leave_notification_bg(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()


class TestSendLeaveNotificationToApplicant(IntegrationTestCase):
    def test_posts_confirmation_dm_to_applicant_with_submission_blocks(self):
        """send_leave_notification_to_applicant posts a chat_postMessage to the applicant's Slack DM with the leave-submission blocks."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        with patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        # The submission blocks include a header that reads "Leave Request Submitted".
        header_text = kwargs["blocks"][0]["text"]["text"]
        self.assertIn("Leave Request Submitted", header_text)

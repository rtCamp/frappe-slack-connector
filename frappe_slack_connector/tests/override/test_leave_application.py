from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.override.leave_application import (
    after_insert,
    send_leave_notification_bg,
    send_leave_notification_to_applicant,
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
):
    """Build a MagicMock that mimics a Leave Application doc with the fields the override code reads."""
    doc = MagicMock()
    doc.name = name
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


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg and send_leave_notification_to_applicant, both on the short queue."""
        doc = _build_leave_doc()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        targets = [call.args[0] for call in mock_enqueue.call_args_list]
        self.assertIn(send_leave_notification_bg, targets)
        self.assertIn(send_leave_notification_to_applicant, targets)
        for call in mock_enqueue.call_args_list:
            self.assertEqual(call.kwargs["queue"], "short")
            self.assertIs(call.kwargs["doc"], doc)


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

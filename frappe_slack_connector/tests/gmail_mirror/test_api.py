import contextlib
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.gmail_mirror import MIRROR
from frappe_slack_connector.gmail_mirror.api import get_status, retry, start, stop
from frappe_slack_connector.tests import (
    TEST_SLACK_CHANNEL_ID,
    TEST_USER,
    as_user,
    build_slack_client_mock,
    make_test_slack_channel,
    make_test_user,
)
from frappe_slack_connector.tests.gmail_mirror import (
    api_error,
    channel_info,
    make_test_mirror,
    make_test_thread,
    requires_gmail_thread,
    set_mirroring,
)

API = "frappe_slack_connector.gmail_mirror.api"
CHANNEL = "test-fsc"


@contextlib.contextmanager
def _no_lock(name, timeout):
    yield


class _ApiTestCase(IntegrationTestCase):
    """Slack is a mock, the job is not queued, and the lock (which commits) is held back."""

    def setUp(self):
        super().setUp()
        set_mirroring(True)
        make_test_slack_channel(channel_name=CHANNEL)
        self.client = build_slack_client_mock()
        for name, replacement in (
            ("mirror_client", {"return_value": self.client}),
            ("enqueue_post", {}),
            ("job_lock", {"new": _no_lock}),
            ("generate_error_log", {}),
        ):
            patcher = patch(f"{API}.{name}", **replacement)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def mirrors_of(self, thread):
        return frappe.db.count(MIRROR, {"gmail_thread": thread.name})


@requires_gmail_thread
class TestStart(_ApiTestCase):
    def test_creates_one_active_mirror_and_queues_the_job(self):
        thread = make_test_thread("fsc-api-start")
        name = start(thread.name, CHANNEL)
        row = frappe.db.get_value(MIRROR, name, ["gmail_thread", "channel_id", "channel_name", "status"], as_dict=True)
        self.assertEqual(
            (row.gmail_thread, row.channel_id, row.channel_name, row.status),
            (thread.name, TEST_SLACK_CHANNEL_ID, CHANNEL, "Active"),
        )
        self.enqueue_post.assert_called_once_with(name, after_commit=True)
        self.client.chat_postMessage.assert_not_called()

    def test_second_start_is_refused(self):
        thread = make_test_thread("fsc-api-twice")
        start(thread.name, CHANNEL)
        with self.assertRaisesRegex(frappe.ValidationError, "already mirrored in #test-fsc"):
            start(thread.name, CHANNEL)
        self.assertEqual(self.mirrors_of(thread), 1)

    def test_refused_while_a_failed_mirror_exists(self):
        """A failed mirror is retried or stopped, never bypassed: a failure must not lead to a second Slack thread."""
        thread = make_test_thread("fsc-api-failed")
        make_test_mirror(thread.name, status="Failed")
        with self.assertRaisesRegex(frappe.ValidationError, "already mirrored"):
            start(thread.name, CHANNEL)

    def test_refusals_from_the_channel(self):
        thread = make_test_thread("fsc-api-channel")
        cases = (
            ({"is_member": False}, "Invite the Slack bot to #test-fsc first"),
            ({"is_ext_shared": True}, "shared with another organisation"),
            ({"is_archived": True}, "is archived"),
        )
        for flags, text in cases:
            with self.subTest(flags=flags):
                self.client.conversations_info.return_value = channel_info(**flags)
                with self.assertRaisesRegex(frappe.ValidationError, text):
                    start(thread.name, CHANNEL)
        self.assertEqual(self.mirrors_of(thread), 0)
        self.enqueue_post.assert_not_called()

    def test_channel_row_holding_a_name_is_resolved_or_refused(self):
        """Some Slack Channel rows hold a name where the ID should be; a name is never stored as the channel ID."""
        thread = make_test_thread("fsc-api-named")
        make_test_slack_channel(channel_id="fsc-named-only", channel_name="fsc-named-only")
        with patch(f"{API}.SlackIntegration") as integration:
            integration.return_value.resolve_channel_id.return_value = "C0REAL0001"
            name = start(thread.name, "fsc-named-only")
            integration.return_value.resolve_channel_id.assert_called_once_with("fsc-named-only")
            self.assertEqual(frappe.db.get_value(MIRROR, name, "channel_id"), "C0REAL0001")
            stop(thread.name)
            integration.return_value.resolve_channel_id.return_value = None
            with self.assertRaisesRegex(frappe.ValidationError, "Could not find Slack channel fsc-named-only"):
                start(thread.name, "fsc-named-only")

    def test_unknown_channel_row_is_refused(self):
        thread = make_test_thread("fsc-api-nochannel")
        with self.assertRaisesRegex(frappe.ValidationError, "Could not find Slack channel fsc-no-such-row"):
            start(thread.name, "fsc-no-such-row")

    def test_needs_write_access_to_the_thread(self):
        thread = make_test_thread("fsc-api-perm")
        make_test_user(TEST_USER)
        with as_user(TEST_USER), self.assertRaises(frappe.PermissionError):
            start(thread.name, CHANNEL)
        self.assertEqual(self.mirrors_of(thread), 0)

    def test_needs_read_access_to_the_channel_row(self):
        user = make_test_user(TEST_USER)
        user.add_roles("Desk User")
        thread = make_test_thread("fsc-api-chanperm", involved_users=[{"account": TEST_USER}])
        with as_user(TEST_USER):
            self.assertTrue(frappe.has_permission("Gmail Thread", "write", doc=thread.name))
            with self.assertRaises(frappe.PermissionError):
                start(thread.name, CHANNEL)
        self.assertEqual(self.mirrors_of(thread), 0)

    def test_refused_when_the_feature_is_off(self):
        thread = make_test_thread("fsc-api-off")
        set_mirroring(False)
        with self.assertRaisesRegex(frappe.ValidationError, "not enabled"):
            start(thread.name, CHANNEL)


@requires_gmail_thread
class TestSendAgain(_ApiTestCase):
    def test_same_channel_resumes_the_stopped_mirror(self):
        thread = make_test_thread("fsc-api-resume")
        mirror = make_test_mirror(thread.name, status="Stopped", thread_ts="7.1")
        self.assertEqual(start(thread.name, CHANNEL), mirror.name)
        row = frappe.db.get_value(MIRROR, mirror.name, ["status", "thread_ts", "retry_count"], as_dict=True)
        self.assertEqual((row.status, row.thread_ts, row.retry_count), ("Active", "7.1", 0))
        self.assertEqual(self.mirrors_of(thread), 1)
        # one note, in the old Slack thread
        self.client.chat_postMessage.assert_called_once()
        note = self.client.chat_postMessage.call_args.kwargs
        self.assertEqual((note["channel"], note["thread_ts"]), (TEST_SLACK_CHANNEL_ID, "7.1"))
        self.assertIn("resumed", note["text"])
        self.enqueue_post.assert_called_once_with(mirror.name, after_commit=True)

    def test_resume_survives_a_failed_note(self):
        thread = make_test_thread("fsc-api-resume-note")
        mirror = make_test_mirror(thread.name, status="Stopped", thread_ts="7.1")
        self.client.chat_postMessage.side_effect = api_error("internal_error")
        self.assertEqual(start(thread.name, CHANNEL), mirror.name)
        self.assertEqual(frappe.db.get_value(MIRROR, mirror.name, "status"), "Active")
        self.generate_error_log.assert_called_once()

    def test_another_channel_gets_a_second_mirror(self):
        thread = make_test_thread("fsc-api-other")
        first = make_test_mirror(thread.name, status="Stopped", thread_ts="7.1")
        make_test_slack_channel(channel_id="C0FSC0009", channel_name="other-fsc")
        second = start(thread.name, "other-fsc")
        self.assertNotEqual(first.name, second)
        self.assertEqual(frappe.db.get_value(MIRROR, second, ["channel_id", "thread_ts"]), ("C0FSC0009", None))
        self.assertEqual(frappe.db.get_value(MIRROR, first.name, "status"), "Stopped")
        self.client.chat_postMessage.assert_not_called()

    def test_mirror_stopped_while_failed_is_not_resumed(self):
        """Stop then Send again is the way out of a Slack thread that is gone."""
        thread = make_test_thread("fsc-api-dead")
        make_test_mirror(thread.name, status="Stopped", thread_ts="7.1", last_error="Slack error: thread_not_found")
        name = start(thread.name, CHANNEL)
        self.assertEqual(self.mirrors_of(thread), 2)
        self.assertIsNone(frappe.db.get_value(MIRROR, name, "thread_ts"))

    def test_mirror_stopped_before_its_parent_was_posted_is_not_resumed(self):
        thread = make_test_thread("fsc-api-noparent")
        first = start(thread.name, CHANNEL)
        stop(thread.name)
        second = start(thread.name, CHANNEL)
        self.assertNotEqual(first, second)
        self.assertEqual(frappe.db.get_value(MIRROR, first, "status"), "Stopped")


@requires_gmail_thread
class TestStopAndRetry(_ApiTestCase):
    def test_stop_posts_one_note_in_the_slack_thread(self):
        thread = make_test_thread("fsc-api-stop")
        mirror = make_test_mirror(thread.name, thread_ts="5.1")
        stop(thread.name)
        self.assertEqual(frappe.db.get_value(MIRROR, mirror.name, "status"), "Stopped")
        self.client.chat_postMessage.assert_called_once()
        note = self.client.chat_postMessage.call_args.kwargs
        self.assertEqual((note["channel"], note["thread_ts"]), (TEST_SLACK_CHANNEL_ID, "5.1"))
        self.assertIn("stopped", note["text"])

    def test_stop_survives_a_slack_failure(self):
        thread = make_test_thread("fsc-api-stop-fail")
        mirror = make_test_mirror(thread.name, thread_ts="5.1")
        self.client.chat_postMessage.side_effect = api_error("internal_error")
        stop(thread.name)
        self.assertEqual(frappe.db.get_value(MIRROR, mirror.name, "status"), "Stopped")
        self.generate_error_log.assert_called_once()

    def test_stop_before_the_parent_was_posted_posts_nothing(self):
        thread = make_test_thread("fsc-api-stop-early")
        make_test_mirror(thread.name)
        stop(thread.name)
        self.client.chat_postMessage.assert_not_called()

    def test_stop_works_on_a_failed_mirror_and_keeps_its_error(self):
        thread = make_test_thread("fsc-api-stop-failed")
        mirror = make_test_mirror(thread.name, status="Failed", last_error="boom", thread_ts="5.1")
        stop(thread.name)
        self.assertEqual(frappe.db.get_value(MIRROR, mirror.name, ["status", "last_error"]), ("Stopped", "boom"))

    def test_stop_without_a_mirror_is_refused(self):
        thread = make_test_thread("fsc-api-stop-none")
        with self.assertRaisesRegex(frappe.ValidationError, "not mirrored"):
            stop(thread.name)

    def test_stop_needs_write_access_to_the_thread(self):
        thread = make_test_thread("fsc-api-stop-perm")
        mirror = make_test_mirror(thread.name)
        make_test_user(TEST_USER)
        with as_user(TEST_USER), self.assertRaises(frappe.PermissionError):
            stop(thread.name)
        self.assertEqual(frappe.db.get_value(MIRROR, mirror.name, "status"), "Active")

    def test_retry_reactivates_a_failed_mirror_in_place(self):
        thread = make_test_thread("fsc-api-retry")
        mirror = make_test_mirror(thread.name, status="Failed", retry_count=5, last_error="x", thread_ts="6.1")
        retry(thread.name)
        row = frappe.db.get_value(
            MIRROR, mirror.name, ["status", "retry_count", "last_error", "thread_ts"], as_dict=True
        )
        self.assertEqual((row.status, row.retry_count, row.last_error or "", row.thread_ts), ("Active", 0, "", "6.1"))
        self.enqueue_post.assert_called_once_with(mirror.name, after_commit=True)
        self.assertEqual(self.mirrors_of(thread), 1)

    def test_retry_on_an_active_mirror_is_refused(self):
        thread = make_test_thread("fsc-api-retry-active")
        make_test_mirror(thread.name)
        with self.assertRaisesRegex(frappe.ValidationError, "nothing to retry"):
            retry(thread.name)
        self.enqueue_post.assert_not_called()


@requires_gmail_thread
class TestGetStatus(_ApiTestCase):
    def test_reports_the_live_mirror_then_the_last_stopped_one(self):
        thread = make_test_thread("fsc-api-status")
        self.assertEqual(get_status(thread.name), {"enabled": True, "mirror": None})
        stopped = make_test_mirror(thread.name, status="Stopped")
        self.assertEqual(get_status(thread.name)["mirror"]["name"], stopped.name)
        failed = make_test_mirror(thread.name, status="Failed", last_error="boom", retry_count=5)
        self.assertEqual(
            get_status(thread.name)["mirror"],
            {"name": failed.name, "status": "Failed", "channel_name": CHANNEL, "last_error": "boom", "retry_count": 5},
        )

    def test_feature_off_reports_nothing(self):
        thread = make_test_thread("fsc-api-status-off")
        make_test_mirror(thread.name)
        set_mirroring(False)
        self.assertEqual(get_status(thread.name), {"enabled": False, "mirror": None})

    def test_needs_read_access_to_the_thread(self):
        thread = make_test_thread("fsc-api-status-perm")
        make_test_user(TEST_USER)
        with as_user(TEST_USER), self.assertRaises(frappe.PermissionError):
            get_status(thread.name)

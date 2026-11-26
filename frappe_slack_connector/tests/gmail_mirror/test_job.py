import contextlib
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_lock import LockTimeoutError

from frappe_slack_connector.gmail_mirror import MIRROR, MIRROR_MESSAGE
from frappe_slack_connector.gmail_mirror.job import MAX_RETRIES, POST_INTERVAL_SECONDS, post_missing, retry_pending
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, build_slack_client_mock
from frappe_slack_connector.tests.gmail_mirror import (
    add_test_email,
    api_error,
    channel_info,
    make_test_mirror,
    make_test_thread,
    requires_gmail_thread,
    set_mirroring,
)

JOB = "frappe_slack_connector.gmail_mirror.job"


@contextlib.contextmanager
def _no_lock(name, timeout):
    yield


def _posted_keys(mirror):
    return frappe.get_all(MIRROR_MESSAGE, filters={"parent": mirror}, order_by="idx", pluck="email_key")


def _state(mirror):
    return frappe.db.get_value(MIRROR, mirror, ["status", "retry_count", "last_error", "thread_ts"], as_dict=True)


class _JobTestCase(IntegrationTestCase):
    """
    The Slack client is a mock that hands out a new ts per post. Commits and
    rollbacks are held back (the fixtures live in the test's open transaction
    and a real rollback in the failure path would erase them), and so is the lock.
    """

    def setUp(self):
        super().setUp()
        set_mirroring(True)
        self.client = build_slack_client_mock()
        self.posts = 0

        def post(**kwargs):
            self.posts += 1
            return {"ok": True, "ts": f"1700000000.{self.posts:06d}"}

        self.client.chat_postMessage.side_effect = post
        for target, replacement in (
            (f"{JOB}.mirror_client", {"return_value": self.client}),
            (f"{JOB}.time.sleep", {}),
            (f"{JOB}.frappe.db.commit", {}),
            (f"{JOB}.frappe.db.rollback", {}),
            (f"{JOB}.job_lock", {"new": _no_lock}),
            (f"{JOB}.generate_error_log", {}),
        ):
            patcher = patch(target, **replacement)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, target.rsplit(".", 1)[1], mock)

    def calls(self):
        return self.client.chat_postMessage.call_args_list


@requires_gmail_thread
class TestPostMissing(_JobTestCase):
    def test_first_run_posts_parent_then_one_reply_per_email_in_order(self):
        thread = make_test_thread("fsc-job-first", emails=3)
        mirror = make_test_mirror(thread.name)
        post_missing(mirror.name)
        calls = self.calls()
        self.assertEqual(len(calls), 4)
        self.assertNotIn("thread_ts", calls[0].kwargs)
        self.assertEqual({c.kwargs["channel"] for c in calls}, {TEST_SLACK_CHANNEL_ID})
        thread_ts = _state(mirror.name).thread_ts
        self.assertEqual(thread_ts, "1700000000.000001")
        self.assertEqual([c.kwargs["thread_ts"] for c in calls[1:]], [thread_ts] * 3)
        self.assertEqual([c.kwargs["text"] for c in calls[1:]], [f"Sender {n}: Body {n}" for n in (1, 2, 3)])
        self.assertEqual(_posted_keys(mirror.name), [f"fsc-job-first-m{n}" for n in (1, 2, 3)])
        self.assertEqual(
            frappe.get_all(MIRROR_MESSAGE, filters={"parent": mirror.name}, order_by="idx", pluck="slack_ts"),
            ["1700000000.000002", "1700000000.000003", "1700000000.000004"],
        )
        for call in calls:
            self.assertIs(call.kwargs["unfurl_links"], False)
            self.assertIs(call.kwargs["unfurl_media"], False)

    def test_pauses_between_replies_but_not_before_the_first(self):
        thread = make_test_thread("fsc-job-pace", emails=3)
        post_missing(make_test_mirror(thread.name).name)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(POST_INTERVAL_SECONDS,)] * 2)

    def test_second_run_posts_nothing(self):
        thread = make_test_thread("fsc-job-twice", emails=2)
        mirror = make_test_mirror(thread.name)
        post_missing(mirror.name)
        post_missing(mirror.name)
        self.assertEqual(len(self.calls()), 3)

    def test_new_email_adds_exactly_one_reply_to_the_same_thread(self):
        thread = make_test_thread("fsc-job-new", emails=2)
        mirror = make_test_mirror(thread.name)
        post_missing(mirror.name)
        add_test_email(thread, 3)
        post_missing(mirror.name)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(self.calls()[-1].kwargs["thread_ts"], _state(mirror.name).thread_ts)
        self.assertEqual(self.calls()[-1].kwargs["text"], "Sender 3: Body 3")

    def test_emails_are_posted_oldest_first_whatever_their_row_order(self):
        thread = make_test_thread("fsc-job-order", emails=2)
        add_test_email(thread, 3, date_and_time="2026-01-01 08:00:00")
        post_missing(make_test_mirror(thread.name).name)
        self.assertEqual([c.kwargs["text"][:8] for c in self.calls()[1:]], ["Sender 3", "Sender 1", "Sender 2"])

    def test_never_writes_to_the_gmail_thread(self):
        thread = make_test_thread("fsc-job-readonly", emails=2)
        before = frappe.db.get_value("Gmail Thread", thread.name, "modified")
        post_missing(make_test_mirror(thread.name).name)
        self.assertEqual(frappe.db.get_value("Gmail Thread", thread.name, "modified"), before)

    def test_thread_without_emails_posts_only_the_parent(self):
        thread = make_test_thread("fsc-job-empty", emails=0)
        mirror = make_test_mirror(thread.name)
        post_missing(mirror.name)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(_state(mirror.name).status, "Active")

    def test_failed_or_stopped_mirror_is_left_alone(self):
        thread = make_test_thread("fsc-job-idle", emails=2)
        for status in ("Failed", "Stopped"):
            with self.subTest(status=status):
                post_missing(make_test_mirror(thread.name, status=status).name)
                self.client.chat_postMessage.assert_not_called()
                self.client.conversations_info.assert_not_called()

    def test_does_nothing_when_the_feature_is_off(self):
        thread = make_test_thread("fsc-job-off", emails=2)
        mirror = make_test_mirror(thread.name)
        set_mirroring(False)
        post_missing(mirror.name)
        self.client.chat_postMessage.assert_not_called()

    def test_missing_mirror_is_ignored(self):
        post_missing("SLACK-MIRROR-GONE")
        self.client.chat_postMessage.assert_not_called()
        self.generate_error_log.assert_not_called()

    def test_stop_during_a_run_leaves_the_rest_unposted(self):
        thread = make_test_thread("fsc-job-stop", emails=3)
        mirror = make_test_mirror(thread.name)

        def post(**kwargs):
            if "thread_ts" in kwargs:  # the first reply: someone presses Stop right after it
                frappe.db.set_value(MIRROR, mirror.name, "status", "Stopped")
            return {"ok": True, "ts": "2.1"}

        self.client.chat_postMessage.side_effect = post
        post_missing(mirror.name)
        self.assertEqual(_posted_keys(mirror.name), ["fsc-job-stop-m1"])
        self.assertEqual(_state(mirror.name).status, "Stopped")


@requires_gmail_thread
class TestFailures(_JobTestCase):
    def test_temporary_failure_keeps_the_mirror_active_and_resumes_in_the_same_thread(self):
        thread = make_test_thread("fsc-job-resume", emails=3)
        mirror = make_test_mirror(thread.name)
        self.client.chat_postMessage.side_effect = [
            {"ok": True, "ts": "1.1"},
            {"ok": True, "ts": "1.2"},
            api_error("internal_error"),
        ]
        post_missing(mirror.name)
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count, state.thread_ts), ("Active", 1, "1.1"))
        self.assertEqual(state.last_error, "Slack error: internal_error")
        self.assertEqual(_posted_keys(mirror.name), ["fsc-job-resume-m1"])

        self.client.chat_postMessage.side_effect = [{"ok": True, "ts": "1.3"}, {"ok": True, "ts": "1.4"}]
        post_missing(mirror.name)
        retried = self.calls()[3:]
        # the same Slack thread, no second parent message
        self.assertEqual([c.kwargs["thread_ts"] for c in retried], ["1.1", "1.1"])
        self.assertEqual(_posted_keys(mirror.name), [f"fsc-job-resume-m{n}" for n in (1, 2, 3)])
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count, state.last_error or ""), ("Active", 0, ""))

    def test_failed_parent_post_is_retried_as_a_parent(self):
        thread = make_test_thread("fsc-job-parent", emails=1)
        mirror = make_test_mirror(thread.name)
        self.client.chat_postMessage.side_effect = api_error("internal_error")
        post_missing(mirror.name)
        self.assertIsNone(_state(mirror.name).thread_ts)
        self.client.chat_postMessage.side_effect = [{"ok": True, "ts": "3.1"}, {"ok": True, "ts": "3.2"}]
        post_missing(mirror.name)
        self.assertEqual(_state(mirror.name).thread_ts, "3.1")
        self.assertEqual(_posted_keys(mirror.name), ["fsc-job-parent-m1"])

    def test_last_allowed_failed_run_in_a_row_fails_the_mirror(self):
        thread = make_test_thread("fsc-job-giveup", emails=1)
        mirror = make_test_mirror(thread.name, retry_count=MAX_RETRIES - 1)
        self.client.chat_postMessage.side_effect = api_error("internal_error")
        post_missing(mirror.name)
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count), ("Failed", MAX_RETRIES))

    def test_permanent_slack_error_fails_at_once_with_a_readable_reason(self):
        thread = make_test_thread("fsc-job-fatal", emails=1)
        mirror = make_test_mirror(thread.name)
        self.client.chat_postMessage.side_effect = api_error("not_in_channel")
        post_missing(mirror.name)
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count), ("Failed", 0))
        self.assertEqual(state.last_error, "Invite the Slack bot to #test-fsc first.")

    def test_channel_shared_externally_after_start_fails_and_posts_nothing(self):
        thread = make_test_thread("fsc-job-shared", emails=2)
        mirror = make_test_mirror(thread.name, thread_ts="4.1")
        self.client.conversations_info.return_value = channel_info(is_ext_shared=True)
        post_missing(mirror.name)
        self.client.chat_postMessage.assert_not_called()
        state = _state(mirror.name)
        self.assertEqual(state.status, "Failed")
        self.assertIn("shared with another organisation", state.last_error)

    def test_deleted_thread_fails_the_mirror(self):
        mirror = make_test_mirror("fsc-job-gone")
        post_missing(mirror.name)
        state = _state(mirror.name)
        self.assertEqual((state.status, state.last_error), ("Failed", "The email thread no longer exists."))

    def test_lock_timeout_counts_as_a_temporary_failure(self):
        thread = make_test_thread("fsc-job-lock", emails=1)
        mirror = make_test_mirror(thread.name)
        with patch(f"{JOB}.job_lock", side_effect=LockTimeoutError):
            post_missing(mirror.name)
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count), ("Active", 1))
        self.assertEqual(state.last_error, "Timed out waiting for an earlier run of this mirror.")

    def test_unexpected_error_is_swallowed_logged_once_and_retried(self):
        thread = make_test_thread("fsc-job-boom", emails=1)
        mirror = make_test_mirror(thread.name)
        self.client.chat_postMessage.side_effect = RuntimeError("boom")
        post_missing(mirror.name)
        self.generate_error_log.assert_called_once()
        self.rollback.assert_called_once()
        state = _state(mirror.name)
        self.assertEqual((state.status, state.retry_count, state.last_error), ("Active", 1, "boom"))

    def test_failure_does_not_reopen_a_mirror_stopped_meanwhile(self):
        thread = make_test_thread("fsc-job-stopfail", emails=1)
        mirror = make_test_mirror(thread.name)

        def post(**kwargs):
            frappe.db.set_value(MIRROR, mirror.name, "status", "Stopped")
            raise api_error("not_in_channel")

        self.client.chat_postMessage.side_effect = post
        post_missing(mirror.name)
        self.assertEqual(_state(mirror.name).status, "Stopped")


@requires_gmail_thread
class TestRetryPending(_JobTestCase):
    def test_queues_only_active_mirrors_with_a_failed_run(self):
        waiting = make_test_mirror("fsc-retry-1", retry_count=2)
        others = [
            make_test_mirror("fsc-retry-2"),
            make_test_mirror("fsc-retry-3", status="Failed", retry_count=MAX_RETRIES),
            make_test_mirror("fsc-retry-4", status="Stopped", retry_count=1),
        ]
        with patch(f"{JOB}.enqueue_post") as enqueue:
            retry_pending()
        # other tests of this class leave mirrors behind in the same transaction, so only these four are judged
        queued = [c.args[0] for c in enqueue.call_args_list]
        self.assertEqual(queued.count(waiting.name), 1)
        self.assertFalse(set(queued) & {other.name for other in others})

    def test_queues_nothing_when_the_feature_is_off(self):
        make_test_mirror("fsc-retry-off", retry_count=2)
        set_mirroring(False)
        with patch(f"{JOB}.enqueue_post") as enqueue:
            retry_pending()
        enqueue.assert_not_called()

    def test_enqueue_post_uses_the_long_queue_without_deduplication(self):
        """No job id: the lock serialises runs, and dropping a duplicate would lose an email that arrives mid-run."""
        from frappe_slack_connector.gmail_mirror.job import enqueue_post

        with patch(f"{JOB}.frappe.enqueue") as enqueue:
            enqueue_post("SLACK-MIRROR-00001", after_commit=True)
        enqueue.assert_called_once_with(
            f"{JOB}.post_missing", queue="long", mirror="SLACK-MIRROR-00001", enqueue_after_commit=True
        )

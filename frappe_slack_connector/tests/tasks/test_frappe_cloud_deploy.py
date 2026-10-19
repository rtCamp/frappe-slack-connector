from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from frappe_slack_connector.tasks.frappe_cloud_deploy import (
    THREAD_DOCTYPE,
    THREAD_WINDOW_HOURS,
    get_watch,
    process_event,
)
from frappe_slack_connector.tests import (
    TEST_SLACK_CHANNEL_ID,
    TEST_SLACK_WORKLOAD_CHANNEL_ID,
    build_slack_client_mock,
)

DEPLOY_MODULE = "frappe_slack_connector.tasks.frappe_cloud_deploy"
TEST_GROUP = "bench-test-fsc"
TEST_DEPLOY = "deploy-test-fsc-000001"
TEST_SITE = "fsc-test.frappe.cloud"
PARENT_TS = "1700000000.000001"


def _row(bench_group, sites="", channel_id=""):
    return frappe._dict(bench_group=bench_group, sites=sites, channel_id=channel_id)


def _build_settings_mock(*, enabled=1, rows=None):
    settings = MagicMock()
    settings.send_frappe_cloud_deploy_updates = enabled
    settings.frappe_cloud_channel_id = TEST_SLACK_CHANNEL_ID
    settings.frappe_cloud_bench_groups = rows or []
    return settings


def _deploy_event(status, **data):
    return {
        "event": "Bench Deploy Status Update",
        "data": {"name": TEST_DEPLOY, "group": TEST_GROUP, "status": status, **data},
    }


def _bench_event(status, bench="bench-test-fsc-000001-f2-london"):
    return {
        "event": "Bench Status Update",
        "data": {"name": bench, "group": TEST_GROUP, "status": status, "cluster": "London"},
    }


def _site_event(status, site=TEST_SITE):
    return {
        "event": "Site Status Update",
        "data": {"doctype": "Site", "name": site, "group": TEST_GROUP, "status": status},
    }


class TestProcessFrappeCloudEvent(IntegrationTestCase):
    def setUp(self):
        frappe.db.delete(THREAD_DOCTYPE, {"bench_group": TEST_GROUP})
        self.client = build_slack_client_mock()
        slack = MagicMock()
        slack.slack_app.client = self.client
        self.settings = _build_settings_mock()
        self.patches = [
            patch(f"{DEPLOY_MODULE}.SlackIntegration", return_value=slack),
            patch(f"{DEPLOY_MODULE}.frappe.get_single", return_value=self.settings),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        frappe.db.delete(THREAD_DOCTYPE, {"bench_group": TEST_GROUP})

    def _replies(self):
        """Texts of the messages posted as thread replies."""
        return [c.kwargs["text"] for c in self.client.chat_postMessage.call_args_list if c.kwargs.get("thread_ts")]

    def test_first_deploy_event_posts_parent_and_reply(self):
        """The first event of a deploy posts a parent message, replies in its thread and stores the thread."""
        process_event(_deploy_event("Pending"))

        parent_call = self.client.chat_postMessage.call_args_list[0]
        self.assertEqual(parent_call.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertNotIn("thread_ts", parent_call.kwargs)
        self.assertIn(TEST_DEPLOY, parent_call.kwargs["text"])
        self.assertIn(f"/groups/{TEST_GROUP}/deploys/{TEST_DEPLOY}", parent_call.kwargs["text"])
        self.assertEqual(len(self._replies()), 1)

        thread = frappe.get_doc(THREAD_DOCTYPE, TEST_DEPLOY)
        self.assertEqual(thread.thread_ts, PARENT_TS)
        self.assertEqual(thread.status, "Pending")

    def test_later_deploy_event_updates_parent_and_replies_in_thread(self):
        """A later status updates the parent message and replies in the existing thread."""
        process_event(_deploy_event("Pending"))
        process_event(_deploy_event("Success", build_duration="0:00:41.794537"))

        self.client.chat_update.assert_called_once()
        self.assertEqual(self.client.chat_update.call_args.kwargs["ts"], PARENT_TS)
        self.assertIn("Success", self.client.chat_update.call_args.kwargs["text"])
        self.assertEqual(self._replies()[-1], ":white_check_mark: Build is *Success* in 0:00:41")
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "status"), "Success")

    def test_channel_name_setting_is_replaced_by_channel_id(self):
        """When the setting holds a channel name, the thread stores the ID Slack returns so chat.update works."""
        self.settings.frappe_cloud_channel_id = "frappe-cloud"
        self.client.chat_postMessage.return_value = {"ok": True, "ts": PARENT_TS, "channel": TEST_SLACK_CHANNEL_ID}
        process_event(_deploy_event("Pending"))
        process_event(_deploy_event("Running"))

        self.assertEqual(self.client.chat_update.call_args.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "channel_id"), TEST_SLACK_CHANNEL_ID)

    def test_failure_reply_includes_build_error(self):
        """A failed build reply includes the build error and it is stored on the thread."""
        process_event(_deploy_event("Pending"))
        process_event(_deploy_event("Failure", build_error="pip install failed"))

        self.assertIn("pip install failed", self._replies()[-1])
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "build_error"), "pip install failed")

    def test_duplicate_deploy_event_is_ignored(self):
        """A redelivered deploy status is not posted twice."""
        process_event(_deploy_event("Running"))
        process_event(_deploy_event("Running"))

        self.assertEqual(self.client.chat_postMessage.call_count, 2)  # parent + one reply
        self.client.chat_update.assert_not_called()

    def test_bench_event_replies_in_latest_thread(self):
        """A bench status change of the bench group is posted in the latest deploy thread, once."""
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active"))
        process_event(_bench_event("Active"))

        self.assertEqual(
            self._replies()[-1], ":white_check_mark: Bench `bench-test-fsc-000001-f2-london` (London) is *Active*"
        )
        self.assertEqual(len(self._replies()), 2)

    def test_bench_event_with_unreported_status_is_ignored(self):
        """Bench statuses outside the reported set, like Archived, are not posted."""
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Archived"))

        self.assertEqual(len(self._replies()), 1)

    def test_bench_event_without_thread_is_ignored(self):
        """A bench event for a bench group with no deploy thread posts nothing."""
        process_event(_bench_event("Active"))

        self.client.chat_postMessage.assert_not_called()

    def test_bench_event_ignores_thread_older_than_window(self):
        """A bench event is not posted in a deploy thread started before the window."""
        process_event(_deploy_event("Success"))
        frappe.db.set_value(
            THREAD_DOCTYPE,
            TEST_DEPLOY,
            "creation",
            add_to_date(now_datetime(), hours=-(THREAD_WINDOW_HOURS + 1)),
            update_modified=False,
        )
        process_event(_bench_event("Active"))

        self.assertEqual(len(self._replies()), 1)

    def test_site_event_replies_in_latest_thread(self):
        """A site status change is posted in the latest deploy thread of its bench group."""
        process_event(_deploy_event("Success"))
        process_event(_site_event("Updating"))
        process_event(_site_event("Active"))

        self.assertEqual(
            self._replies()[-2:],
            [
                f":gear: Site `{TEST_SITE}` is *Updating*",
                f":white_check_mark: Site `{TEST_SITE}` is *Active*",
            ],
        )

    def test_unwrapped_site_payload_is_supported(self):
        """A Site Status Update sent as the bare Site document, as in the Frappe Cloud docs, is handled."""
        process_event(_deploy_event("Success"))
        process_event({"doctype": "Site", "name": TEST_SITE, "group": TEST_GROUP, "status": "Active"})

        self.assertIn(TEST_SITE, self._replies()[-1])

    def test_site_not_in_watched_sites_is_ignored(self):
        """Only the listed sites of a watched bench group are posted."""
        self.settings.frappe_cloud_bench_groups = [_row(TEST_GROUP, sites="other.frappe.cloud")]
        process_event(_deploy_event("Success"))
        process_event(_site_event("Active"))

        self.assertEqual(len(self._replies()), 1)

    def test_unwatched_bench_group_is_ignored(self):
        """Events of a bench group missing from the watch table post nothing."""
        self.settings.frappe_cloud_bench_groups = [_row("bench-other")]
        process_event(_deploy_event("Pending"))

        self.client.chat_postMessage.assert_not_called()
        self.assertFalse(frappe.db.exists(THREAD_DOCTYPE, TEST_DEPLOY))

    def test_returns_silently_when_disabled(self):
        """Nothing is posted when Frappe Cloud deploy updates are switched off."""
        self.settings.send_frappe_cloud_deploy_updates = 0
        process_event(_deploy_event("Pending"))

        self.client.chat_postMessage.assert_not_called()

    def test_slack_error_is_logged_and_thread_not_saved(self):
        """A Slack API error is logged and the thread is rolled back so the event is not marked as posted."""
        self.client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
        with patch(f"{DEPLOY_MODULE}.generate_error_log") as mock_log:
            process_event(_deploy_event("Pending"))

        mock_log.assert_called_once()
        self.assertFalse(frappe.db.exists(THREAD_DOCTYPE, TEST_DEPLOY))


class TestGetWatch(IntegrationTestCase):
    def test_empty_table_watches_every_group_on_default_channel(self):
        """With no watched bench groups, every group is reported on the default channel for all sites."""
        watch = get_watch(_build_settings_mock(), TEST_GROUP)
        self.assertEqual(watch.channel_id, TEST_SLACK_CHANNEL_ID)
        self.assertEqual(watch.sites, set())

    def test_row_channel_and_sites_override_defaults(self):
        """A watched row uses its own channel and parses one site per line."""
        settings = _build_settings_mock(
            rows=[_row(TEST_GROUP, sites=f" {TEST_SITE}\n\nb.frappe.cloud ", channel_id=TEST_SLACK_WORKLOAD_CHANNEL_ID)]
        )
        watch = get_watch(settings, TEST_GROUP)
        self.assertEqual(watch.channel_id, TEST_SLACK_WORKLOAD_CHANNEL_ID)
        self.assertEqual(watch.sites, {TEST_SITE, "b.frappe.cloud"})

    def test_returns_none_without_group(self):
        """Events with no bench group are never watched."""
        self.assertIsNone(get_watch(_build_settings_mock(), None))

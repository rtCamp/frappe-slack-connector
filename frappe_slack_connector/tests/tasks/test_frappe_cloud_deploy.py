from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from frappe_slack_connector.tasks.frappe_cloud_deploy import (
    PARENT_TEXT,
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
TEST_DEPLOY = "fsctestbuild1"
TEST_CANDIDATE = "deploy-99999-000005"
TEST_BENCH = "bench-99999-000005-f2-london"
OLD_DEPLOY = "fsctestbuild0"
OLD_CANDIDATE = "deploy-99999-000004"
OLD_BENCH = "bench-99999-000004-f2-london"
TEST_SITE = "fsc-test.frappe.cloud"
TEST_SITE_2 = "fsc-test-2.frappe.cloud"
PARENT_TS = "1700000000.000001"


def _row(bench_group, sites="", channel_id=""):
    return frappe._dict(bench_group=bench_group, sites=sites, channel_id=channel_id)


def _build_settings_mock(*, enabled=1, rows=None):
    settings = MagicMock()
    settings.send_frappe_cloud_deploy_updates = enabled
    settings.frappe_cloud_channel_id = TEST_SLACK_CHANNEL_ID
    settings.frappe_cloud_bench_groups = rows or []
    return settings


def _deploy_event(status, deploy=TEST_DEPLOY, candidate=TEST_CANDIDATE, **data):
    return {
        "event": "Bench Deploy Status Update",
        "data": {"name": deploy, "deploy_candidate": candidate, "group": TEST_GROUP, "status": status, **data},
    }


def _bench_event(status, bench=TEST_BENCH):
    return {
        "event": "Bench Status Update",
        "data": {"name": bench, "group": TEST_GROUP, "status": status, "cluster": "London"},
    }


def _site_event(status, site=TEST_SITE, bench=TEST_BENCH):
    return {
        "event": "Site Status Update",
        "data": {"doctype": "Site", "name": site, "group": TEST_GROUP, "bench": bench, "status": status},
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

    def _watch_sites(self, *sites):
        self.settings.frappe_cloud_bench_groups = [_row(TEST_GROUP, sites="\n".join(sites))]

    def _replies(self):
        """Texts of the messages posted as thread replies."""
        return [c.kwargs["text"] for c in self.client.chat_postMessage.call_args_list if c.kwargs.get("thread_ts")]

    def test_build_start_posts_parent_and_started_reply(self):
        """The first started deploy status posts the parent message and one 'started' reply."""
        process_event(_deploy_event("Pending"))

        parent_call = self.client.chat_postMessage.call_args_list[0]
        self.assertEqual(parent_call.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertEqual(parent_call.kwargs["text"], PARENT_TEXT)
        self.assertNotIn("thread_ts", parent_call.kwargs)
        self.assertEqual(len(self._replies()), 1)
        self.assertIn("Bench update started", self._replies()[0])
        self.assertIn(f"/groups/{TEST_GROUP}/deploys/{TEST_DEPLOY}", self._replies()[0])
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "thread_ts"), PARENT_TS)

    def test_build_progress_and_success_post_nothing_more(self):
        """Later build statuses, including Success, add no messages to the thread."""
        process_event(_deploy_event("Pending"))
        process_event(_deploy_event("Preparing"))
        process_event(_deploy_event("Running"))
        process_event(_deploy_event("Success", build_duration="0:00:41.794537"))

        self.assertEqual(self.client.chat_postMessage.call_count, 2)  # parent + started
        self.client.chat_update.assert_not_called()
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "status"), "Success")

    def test_draft_and_scheduled_deploys_do_not_start_a_thread(self):
        """A deploy that has not started building yet posts nothing and creates no thread."""
        process_event(_deploy_event("Draft"))
        process_event(_deploy_event("Scheduled"))

        self.client.chat_postMessage.assert_not_called()
        self.assertFalse(frappe.db.exists(THREAD_DOCTYPE, TEST_DEPLOY))

    def test_build_failure_posts_failure_with_error(self):
        """A failed build posts one failure reply that includes the build error."""
        process_event(_deploy_event("Running"))
        process_event(_deploy_event("Failure", build_error="pip install failed"))
        process_event(_deploy_event("Failure", build_error="pip install failed"))

        self.assertEqual(len(self._replies()), 2)
        self.assertIn(":x: Bench update failed", self._replies()[-1])
        self.assertIn("pip install failed", self._replies()[-1])
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "build_error"), "pip install failed")

    def test_channel_name_setting_is_replaced_by_channel_id(self):
        """When the setting holds a channel name, the thread stores the ID Slack returns."""
        self.settings.frappe_cloud_channel_id = "frappe-cloud"
        self.client.chat_postMessage.return_value = {"ok": True, "ts": PARENT_TS, "channel": TEST_SLACK_CHANNEL_ID}
        process_event(_deploy_event("Pending"))

        self.assertEqual(self.client.chat_postMessage.call_args.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "channel_id"), TEST_SLACK_CHANNEL_ID)

    def test_bench_active_completes_update_without_watched_sites(self):
        """With no sites to wait for, the update is complete once the new bench is live, posted once."""
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Installing"))
        process_event(_bench_event("Active"))
        process_event(_bench_event("Active"))

        self.assertEqual(len(self._replies()), 2)
        self.assertEqual(self._replies()[-1], ":white_check_mark: Bench update complete, the new bench is live")
        self.assertEqual(frappe.db.get_value(THREAD_DOCTYPE, TEST_DEPLOY, "status"), "Complete")

    def test_watched_sites_complete_update_once_all_back_online(self):
        """With watched sites, the update is complete only after the bench and every site are Active."""
        self._watch_sites(TEST_SITE, TEST_SITE_2)
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active"))
        process_event(_site_event("Active"))
        self.assertEqual(len(self._replies()), 1)

        process_event(_site_event("Active", site=TEST_SITE_2))
        self.assertEqual(len(self._replies()), 2)
        self.assertEqual(self._replies()[-1], ":white_check_mark: Bench update complete, all sites are back online")

    def test_site_active_before_new_bench_is_ignored(self):
        """A site going Active before the new bench is live is not counted towards completion."""
        self._watch_sites(TEST_SITE)
        process_event(_deploy_event("Running"))
        process_event(_site_event("Active"))
        process_event(_bench_event("Active"))

        self.assertEqual(len(self._replies()), 1)  # only started

    def test_unwatched_site_is_ignored(self):
        """Sites not listed for the bench group do not count towards completion."""
        self._watch_sites(TEST_SITE)
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active"))
        process_event(_site_event("Active", site="other.frappe.cloud"))

        self.assertEqual(len(self._replies()), 1)

    def test_unwrapped_site_payload_is_supported(self):
        """A Site Status Update sent as the bare Site document, as in the Frappe Cloud docs, is handled."""
        self._watch_sites(TEST_SITE)
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active"))
        process_event(
            {"doctype": "Site", "name": TEST_SITE, "group": TEST_GROUP, "bench": TEST_BENCH, "status": "Active"}
        )

        self.assertIn("Bench update complete", self._replies()[-1])

    def test_late_events_of_older_deploy_do_not_start_a_thread(self):
        """Retried events of an older deploy arriving after a newer deploy started post nothing."""
        process_event(_deploy_event("Running"))
        process_event(_deploy_event("Pending", deploy=OLD_DEPLOY, candidate=OLD_CANDIDATE))
        process_event(_deploy_event("Running", deploy=OLD_DEPLOY, candidate=OLD_CANDIDATE))

        self.assertEqual(self.client.chat_postMessage.call_count, 2)  # parent + started of the newer deploy
        self.assertFalse(frappe.db.exists(THREAD_DOCTYPE, OLD_DEPLOY))

    def test_bench_of_another_deploy_does_not_complete_the_update(self):
        """A bench built by a different deploy candidate is not applied to this deploy's thread."""
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active", bench=OLD_BENCH))

        self.assertEqual(len(self._replies()), 1)  # only started

    def test_site_on_another_bench_does_not_count(self):
        """A watched site going Active on a bench of another deploy does not complete this update."""
        self._watch_sites(TEST_SITE)
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Active"))
        process_event(_site_event("Active", bench=OLD_BENCH))

        self.assertEqual(len(self._replies()), 1)

    def test_broken_bench_posts_failure(self):
        """A broken new bench posts one failure reply."""
        process_event(_deploy_event("Success"))
        process_event(_bench_event("Broken"))
        process_event(_bench_event("Broken"))

        self.assertEqual(len(self._replies()), 2)
        self.assertEqual(self._replies()[-1], f":x: Bench update failed: the new bench `{TEST_BENCH}` is broken")

    def test_broken_watched_site_posts_failure(self):
        """A watched site reported Broken posts a failure reply."""
        self._watch_sites(TEST_SITE)
        process_event(_deploy_event("Success"))
        process_event(_site_event("Broken"))

        self.assertEqual(self._replies()[-1], f":x: Bench update failed: site `{TEST_SITE}` is broken")

    def test_bench_event_without_thread_is_ignored(self):
        """A bench event for a bench group with no deploy thread posts nothing."""
        process_event(_bench_event("Active"))

        self.client.chat_postMessage.assert_not_called()

    def test_bench_event_ignores_thread_older_than_window(self):
        """A bench event is not applied to a deploy thread started before the window."""
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
        """With no watched bench groups, every group is reported on the default channel with no sites to wait for."""
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

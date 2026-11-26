from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.tests.gmail_mirror import (
    add_test_email,
    make_test_mirror,
    make_test_thread,
    requires_gmail_thread,
    set_mirroring,
)

EVENTS = "frappe_slack_connector.gmail_mirror.events"


@requires_gmail_thread
class TestOnThreadUpdate(IntegrationTestCase):
    """The thread is really saved, the way the Gmail sync saves it, so the hook registration is covered too."""

    def setUp(self):
        super().setUp()
        set_mirroring(True)

    def test_saving_a_mirrored_thread_queues_its_active_mirror(self):
        thread = make_test_thread("fsc-event-active")
        mirror = make_test_mirror(thread.name)
        with patch(f"{EVENTS}.enqueue_post") as enqueue:
            add_test_email(thread, 3)
        enqueue.assert_called_once_with(mirror.name, after_commit=True)

    def test_threads_without_an_active_mirror_queue_nothing(self):
        thread = make_test_thread("fsc-event-idle")
        make_test_mirror(thread.name, status="Stopped")
        make_test_mirror(thread.name, status="Failed")
        with patch(f"{EVENTS}.enqueue_post") as enqueue:
            add_test_email(thread, 3)
            add_test_email(make_test_thread("fsc-event-unmirrored"), 3)
        enqueue.assert_not_called()

    def test_feature_off_queues_nothing(self):
        thread = make_test_thread("fsc-event-off")
        make_test_mirror(thread.name)
        set_mirroring(False)
        with patch(f"{EVENTS}.enqueue_post") as enqueue:
            add_test_email(thread, 3)
        enqueue.assert_not_called()

    def test_a_failure_in_the_hook_does_not_break_the_save(self):
        """The hook runs inside the Gmail sync's save; raising there would make the sync skip the email."""
        thread = make_test_thread("fsc-event-broken")
        make_test_mirror(thread.name)
        with (
            patch(f"{EVENTS}.enqueue_post", side_effect=RuntimeError("queue is down")),
            patch(f"{EVENTS}.generate_error_log") as log,
        ):
            add_test_email(thread, 3)
        self.assertEqual(len(frappe.get_doc("Gmail Thread", thread.name).emails), 3)
        log.assert_called_once()


class TestSchedule(IntegrationTestCase):
    def test_retry_runs_every_ten_minutes(self):
        cron = frappe.get_hooks("scheduler_events")["cron"]
        self.assertIn("frappe_slack_connector.gmail_mirror.job.retry_pending", cron["*/10 * * * *"])

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.db.leave_application import APPLICANT_CHANNEL_FIELD, APPLICANT_MSG_TS_FIELD
from frappe_slack_connector.override.leave_application import (
    APPLICANT_DM_LOCK_TIMEOUT,
    _applicant_dm_lock,
    after_insert,
    on_update_notify_applicant,
    restore_applicant_message_ref,
    send_leave_decision_to_applicant,
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
    status="Open",
    docstatus=0,
    status_changed=False,
    docstatus_changed=False,
    in_insert=False,
    modified_by="approver@x.com",
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
    doc.status = status
    doc.docstatus = docstatus
    doc.modified_by = modified_by
    doc.flags = frappe._dict(in_insert=in_insert)
    changed = {"status": status_changed, "docstatus": docstatus_changed}
    doc.has_value_changed.side_effect = lambda fieldname: changed.get(fieldname, False)
    return doc


def _build_slack_user_lookup(*, employee_slack_id=TEST_SLACK_USER_ID, approver_slack_id="U-approver"):
    """Return a get_slack_user_id side effect that resolves employees and user emails independently."""

    def side_effect(*args, **kwargs):
        if "employee_id" in kwargs:
            return employee_slack_id
        return approver_slack_id

    return side_effect


def _run_enqueued_inline(fn, **kwargs):
    """Stand-in for frappe.enqueue that runs the job synchronously with its kwargs."""
    kwargs.pop("queue", None)
    kwargs.pop("enqueue_after_commit", None)
    return fn(**kwargs)


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
        """after_insert enqueues send_leave_notification_bg and send_leave_notification_to_applicant, both on the short queue and only after the insert commits (the applicant job writes the DM reference back to the row)."""
        doc = _build_leave_doc()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        targets = [call.args[0] for call in mock_enqueue.call_args_list]
        self.assertIn(send_leave_notification_bg, targets)
        self.assertIn(send_leave_notification_to_applicant, targets)
        for call in mock_enqueue.call_args_list:
            self.assertEqual(call.kwargs["queue"], "short")
            self.assertTrue(call.kwargs["enqueue_after_commit"])
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
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"),
        ):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        # The submission blocks include a header that reads "Leave Request Submitted".
        header_text = kwargs["blocks"][0]["text"]["text"]
        self.assertIn("Leave Request Submitted", header_text)

    def test_stores_dm_channel_and_ts_after_posting(self):
        """After the confirmation DM is posted, the returned channel and ts are stored on the Leave Application."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        mock_slack.slack_app.client.chat_postMessage.return_value = {
            "ok": True,
            "channel": "D0FSC0001",
            "ts": "1700000000.000001",
        }
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref") as mock_store,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=None),
        ):
            send_leave_notification_to_applicant(doc)
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="1700000000.000001")
        self.assertEqual(
            mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["text"], "Leave request submitted"
        )
        mock_slack.slack_app.client.chat_update.assert_not_called()

    def test_renders_decision_when_leave_is_already_decided_at_insert(self):
        """A leave created directly as Approved (submitted) gets a confirmation DM that already shows the decision."""
        doc = _build_leave_doc(status="Approved", docstatus=1, modified_by="approver@x.com")
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
        ):
            send_leave_notification_to_applicant(doc)
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["blocks"][0]["text"]["text"], ":white_check_mark: Leave Request Approved")
        self.assertIn("*Status:* Approved by <@U-approver>", kwargs["blocks"][-1]["text"]["text"])
        self.assertEqual(kwargs["text"], "Your leave request has been approved")
        mock_slack.slack_app.client.chat_update.assert_not_called()

    def test_skips_posting_when_decision_job_already_stored_a_dm(self):
        """When the decision job ran first and stored a DM reference, the submission job posts nothing (one message per applicant)."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=("D0FSC0001", "9.9")),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref") as mock_store,
        ):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_store.assert_not_called()

    def test_posts_and_stores_under_the_per_leave_lock(self):
        """The reference check, the post and the store all happen while the per-leave lock is held, so the decision job cannot interleave."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "channel": "D0FSC0001", "ts": "1.1"}
        order = []
        lock = MagicMock()
        lock.__enter__ = MagicMock(side_effect=lambda *a: order.append("lock"))
        lock.__exit__ = MagicMock(side_effect=lambda *a: order.append("unlock"))
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}._applicant_dm_lock", return_value=lock) as mock_lock,
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref",
                side_effect=lambda name: order.append("ref") or (None, None),
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref",
                side_effect=lambda *a, **k: order.append("store"),
            ),
        ):
            mock_slack.slack_app.client.chat_postMessage.side_effect = lambda **k: (
                order.append("post")
                or {
                    "ok": True,
                    "channel": "D0FSC0001",
                    "ts": "1.1",
                }
            )
            send_leave_notification_to_applicant(doc)
        mock_lock.assert_called_once_with(doc.name)
        self.assertEqual(order, ["lock", "ref", "post", "store", "unlock"])

    def test_skips_dm_and_logs_when_employee_has_no_slack_id(self):
        """When the employee has no Slack ID, no chat_postMessage is attempted and the failure is logged."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = None
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_log,
        ):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_logs_and_swallows_slack_errors(self):
        """A Slack API failure while posting the confirmation DM is logged and does not propagate."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        mock_slack.slack_app.client.chat_postMessage.side_effect = RuntimeError("slack down")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_log,
        ):
            send_leave_notification_to_applicant(doc)
        mock_log.assert_called_once()
        self.assertIsInstance(mock_log.call_args.kwargs["exception"], RuntimeError)


class TestOnUpdateNotifyApplicant(IntegrationTestCase):
    def test_enqueues_decision_job_on_submit_with_approved_status(self):
        """Submitting an approved leave (status changed on this save) enqueues send_leave_decision_to_applicant on the short queue after commit."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True, modified_by="approver@x.com")
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], send_leave_decision_to_applicant)
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["queue"], "short")
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertIs(kwargs["doc"], doc)
        self.assertEqual(kwargs["status"], "Approved")
        self.assertEqual(kwargs["decided_by"], "approver@x.com")

    def test_enqueues_once_on_submit_when_status_was_set_in_an_earlier_save(self):
        """Approved set on a draft save then submitted: the submit (docstatus changed, status unchanged) enqueues exactly once."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=False, docstatus_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["status"], "Approved")

    def test_does_not_enqueue_on_draft_save_with_approved_status(self):
        """Approved on a draft (docstatus 0) is not a decision yet: HRMS applies the leave on submit, so nothing is enqueued."""
        doc = _build_leave_doc(status="Approved", docstatus=0, status_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_enqueues_on_draft_save_with_rejected_status(self):
        """Rejected on a draft save (workflows that keep rejected leaves as drafts) enqueues the decision job."""
        doc = _build_leave_doc(status="Rejected", docstatus=0, status_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["status"], "Rejected")

    def test_does_not_enqueue_when_nothing_changed(self):
        """An edit to an already-approved, submitted leave that changes neither status nor docstatus enqueues nothing."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=False, docstatus_changed=False)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_does_not_enqueue_when_status_changes_to_open(self):
        """A status change to a non-decision status (Open) enqueues nothing."""
        doc = _build_leave_doc(status="Open", docstatus=0, status_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_does_not_enqueue_during_insert(self):
        """on_update fired from insert() (flags.in_insert set) enqueues nothing; the submission DM renders the decision instead."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True, in_insert=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_cancelled_passes_cancelling_user_as_decided_by(self):
        """On cancellation the acting user (modified_by) is passed as decided_by."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2, status_changed=True, modified_by="hr@x.com")
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_cancel")
        self.assertEqual(mock_enqueue.call_args.kwargs["status"], "Cancelled")
        self.assertEqual(mock_enqueue.call_args.kwargs["decided_by"], "hr@x.com")

    def test_rejected_save_then_submit_on_same_doc_enqueues_once(self):
        """reject_leave saves (status changed) then submits (docstatus changed) the same doc object; only the first hook call enqueues."""
        doc = _build_leave_doc(status="Rejected", docstatus=0, status_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
            doc.docstatus = 1
            doc.has_value_changed.side_effect = lambda fieldname: fieldname == "docstatus"
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["status"], "Rejected")

    def test_different_status_on_same_doc_enqueues_again(self):
        """An approve followed by a cancel on the same doc object enqueues a decision job for each status."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
            doc.status = "Cancelled"
            doc.docstatus = 2
            on_update_notify_applicant(doc, method="on_cancel")
        self.assertEqual(mock_enqueue.call_count, 2)
        statuses = [call.kwargs["status"] for call in mock_enqueue.call_args_list]
        self.assertEqual(statuses, ["Approved", "Cancelled"])

    def test_cancelled_by_administrator_is_not_attributed_to_the_approver(self):
        """A cancellation made by Administrator passes decided_by=None rather than naming the leave approver."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2, status_changed=True, modified_by="Administrator")
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_cancel")
        self.assertIsNone(mock_enqueue.call_args.kwargs["decided_by"])

    def test_falls_back_to_leave_approver_when_modified_by_is_administrator(self):
        """When the change was made by Administrator (e.g. a background job), decided_by falls back to the leave approver."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True, modified_by="Administrator")
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        self.assertEqual(mock_enqueue.call_args.kwargs["decided_by"], "approver@x.com")

    def test_real_document_submit_enqueues_decision_job(self):
        """A real Leave Application document going Open/draft -> Approved/submitted (doc_before_save set) enqueues through the unpatched has_value_changed."""
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                "employee": "EMP-001",
                "leave_approver": "approver@x.com",
                "modified_by": "approver@x.com",
                "status": "Approved",
                "docstatus": 1,
            }
        )
        doc._doc_before_save = frappe.get_doc({"doctype": "Leave Application", "status": "Open", "docstatus": 0})
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["status"], "Approved")
        self.assertEqual(kwargs["decided_by"], "approver@x.com")
        self.assertTrue(kwargs["enqueue_after_commit"])


class TestRestoreApplicantMessageRef(IntegrationTestCase):
    def test_copies_ref_from_doc_before_save_when_in_memory_values_are_empty(self):
        """A saved doc whose in-memory ref fields are empty gets the channel/ts from the already-loaded doc_before_save, without a second query."""
        doc = frappe.get_doc({"doctype": "Leave Application"})
        doc.name = "HR-LAP-RESTORE"
        doc._doc_before_save = frappe.get_doc(
            {
                "doctype": "Leave Application",
                APPLICANT_CHANNEL_FIELD: "D0FSC0001",
                APPLICANT_MSG_TS_FIELD: "1700000000.000001",
            }
        )
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref") as mock_ref,
        ):
            restore_applicant_message_ref(doc, method="before_validate")
        mock_ref.assert_not_called()
        self.assertEqual(doc.get(APPLICANT_CHANNEL_FIELD), "D0FSC0001")
        self.assertEqual(doc.get(APPLICANT_MSG_TS_FIELD), "1700000000.000001")

    def test_falls_back_to_database_lookup_when_no_doc_before_save(self):
        """Without a loaded doc_before_save, the stored channel/ts are read from the database and copied onto the doc."""
        doc = frappe.get_doc({"doctype": "Leave Application"})
        doc.name = "HR-LAP-RESTORE"
        doc._doc_before_save = None
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref",
                return_value=("D0FSC0001", "1700000000.000001"),
            ) as mock_ref,
        ):
            restore_applicant_message_ref(doc, method="before_validate")
        mock_ref.assert_called_once_with("HR-LAP-RESTORE")
        self.assertEqual(doc.get(APPLICANT_CHANNEL_FIELD), "D0FSC0001")
        self.assertEqual(doc.get(APPLICANT_MSG_TS_FIELD), "1700000000.000001")

    def test_replaces_client_supplied_ref_on_existing_doc(self):
        """Client-supplied ref values on an existing doc are replaced with the committed ones even when non-empty."""
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                APPLICANT_CHANNEL_FIELD: TEST_SLACK_CHANNEL_ID,
                APPLICANT_MSG_TS_FIELD: "1700000000.000009",
            }
        )
        doc.name = "HR-LAP-RESTORE"
        doc._doc_before_save = frappe.get_doc(
            {
                "doctype": "Leave Application",
                APPLICANT_CHANNEL_FIELD: "D0FSC0001",
                APPLICANT_MSG_TS_FIELD: "1700000000.000001",
            }
        )
        with patch.object(doc, "is_new", return_value=False):
            restore_applicant_message_ref(doc, method="before_validate")
        self.assertEqual(doc.get(APPLICANT_CHANNEL_FIELD), "D0FSC0001")
        self.assertEqual(doc.get(APPLICANT_MSG_TS_FIELD), "1700000000.000001")

    def test_clears_client_supplied_ref_when_nothing_is_stored(self):
        """When the committed row holds no ref, client-supplied values on an existing doc are cleared."""
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                APPLICANT_CHANNEL_FIELD: TEST_SLACK_CHANNEL_ID,
                APPLICANT_MSG_TS_FIELD: "1700000000.000009",
            }
        )
        doc.name = "HR-LAP-RESTORE"
        doc._doc_before_save = frappe.get_doc({"doctype": "Leave Application"})
        with patch.object(doc, "is_new", return_value=False):
            restore_applicant_message_ref(doc, method="before_validate")
        self.assertIsNone(doc.get(APPLICANT_CHANNEL_FIELD))
        self.assertIsNone(doc.get(APPLICANT_MSG_TS_FIELD))

    def test_clears_client_supplied_ref_on_new_docs(self):
        """Client-supplied ref values on a document being inserted are discarded and the database is not consulted."""
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                APPLICANT_CHANNEL_FIELD: TEST_SLACK_CHANNEL_ID,
                APPLICANT_MSG_TS_FIELD: "1700000000.000009",
            }
        )
        doc.set("__islocal", True)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref") as mock_ref:
            restore_applicant_message_ref(doc, method="before_validate")
        mock_ref.assert_not_called()
        self.assertIsNone(doc.get(APPLICANT_CHANNEL_FIELD))
        self.assertIsNone(doc.get(APPLICANT_MSG_TS_FIELD))

    def test_clears_client_supplied_ref_on_nameless_docs(self):
        """A document with no name (never inserted) has nothing stored; client values are discarded without a lookup."""
        doc = frappe.get_doc({"doctype": "Leave Application", APPLICANT_CHANNEL_FIELD: TEST_SLACK_CHANNEL_ID})
        with patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref") as mock_ref:
            restore_applicant_message_ref(doc, method="before_validate")
        mock_ref.assert_not_called()
        self.assertIsNone(doc.get(APPLICANT_CHANNEL_FIELD))


class TestDocEventHooks(IntegrationTestCase):
    def test_leave_application_doc_events_are_registered(self):
        """hooks.py registers the applicant-DM handlers on the Leave Application doc events Frappe actually fires."""
        events = frappe.get_hooks("doc_events")["Leave Application"]
        notify = f"{LEAVE_OVERRIDE_MODULE}.on_update_notify_applicant"
        restore = f"{LEAVE_OVERRIDE_MODULE}.restore_applicant_message_ref"
        self.assertIn(notify, events["on_update"])
        self.assertIn(notify, events["on_cancel"])
        self.assertIn(restore, events["before_validate"])
        self.assertIn(restore, events["before_update_after_submit"])
        self.assertIn(restore, events["before_cancel"])


class TestSendLeaveDecisionToApplicant(IntegrationTestCase):
    @contextlib.contextmanager
    def _patched(
        self,
        mock_slack,
        *,
        message_ref=("D0FSC0001", "1700000000.000001"),
        mention_user=1,
        current_status="Approved",
    ):
        """Patch Slack, the stored message ref, the leave's current status, the mention_user setting, the approver full-name lookup, the ref store and the error log; yield (store, log) mocks."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack))
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=message_ref))
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_status", return_value=current_status))
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=mention_user))
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value="Approver Person"))
            mock_store = stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"))
            mock_log = stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log"))
            yield mock_store, mock_log

    def test_slack_approve_updates_applicant_dm_in_place(self):
        """Approving via the Slack button (status changed to Approved through the doc hook) edits the stored applicant DM with chat_update and posts no new message."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            self._patched(mock_slack, mention_user=1),
        ):
            on_update_notify_applicant(doc, method="on_update")
        mock_slack.slack_app.client.chat_update.assert_called_once()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["channel"], "D0FSC0001")
        self.assertEqual(kwargs["ts"], "1700000000.000001")
        self.assertEqual(kwargs["text"], "Your leave request has been approved")
        self.assertIn("Leave Request Approved", kwargs["blocks"][0]["text"]["text"])
        status_line = kwargs["blocks"][-1]["text"]["text"]
        self.assertIn("*Status:* Approved by <@U-approver>", status_line)

    def test_desk_reject_updates_applicant_dm_in_place(self):
        """Rejecting from Desk (status changed to Rejected on submit) edits the stored applicant DM and names the approver when mentions are disabled."""
        doc = _build_leave_doc(status="Rejected", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            self._patched(mock_slack, mention_user=0, current_status="Rejected"),
        ):
            on_update_notify_applicant(doc, method="on_update")
        mock_slack.slack_app.client.chat_update.assert_called_once()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertIn("Leave Request Rejected", kwargs["blocks"][0]["text"]["text"])
        status_line = kwargs["blocks"][-1]["text"]["text"]
        self.assertIn("*Status:* Rejected by Approver Person", status_line)

    def test_cancelled_without_decider_shows_bare_status(self):
        """A cancellation with no attributable user renders the status line as just 'Cancelled'."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, mention_user=1, current_status="Cancelled"):
            send_leave_decision_to_applicant(doc=doc, status="Cancelled", decided_by=None)
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["blocks"][-1]["text"]["text"], "*Status:* Cancelled")

    def test_cancelled_uses_cancelled_header(self):
        """A cancellation rewrites the header to 'Leave Request Cancelled' and names the user who cancelled."""
        doc = _build_leave_doc(status="Cancelled", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, mention_user=0, current_status="Cancelled"):
            send_leave_decision_to_applicant(doc=doc, status="Cancelled", decided_by="hr@x.com")
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["blocks"][0]["text"]["text"], ":no_entry_sign: Leave Request Cancelled")
        self.assertIn("*Status:* Cancelled by Approver Person", kwargs["blocks"][-1]["text"]["text"])

    def test_posts_fresh_dm_when_no_stored_ts(self):
        """A leave with no stored message ts gets a fresh chat_postMessage with the decision instead of chat_update, and the new ref is stored."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_postMessage.return_value = {
            "ok": True,
            "channel": "D0FSC0001",
            "ts": "1700000000.000009",
        }
        with self._patched(mock_slack, message_ref=(None, None)) as (mock_store, _):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        self.assertIn("Leave Request Approved", kwargs["blocks"][0]["text"]["text"])
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="1700000000.000009")

    def test_no_slack_id_logs_and_makes_no_slack_call(self):
        """An employee with no Slack ID causes no exception and no Slack call; the failure is logged."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup(employee_slack_id=None)
        with self._patched(mock_slack) as (_, mock_log):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_falls_back_to_full_name_when_approver_has_no_slack_id(self):
        """When mentions are enabled but the approver has no Slack ID, the status line uses the approver's full name."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup(approver_slack_id=None)
        with self._patched(mock_slack, mention_user=1):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertIn("*Status:* Approved by Approver Person", kwargs["blocks"][-1]["text"]["text"])

    def test_logs_and_swallows_slack_errors(self):
        """A Slack API failure while updating the DM is logged and does not propagate."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_update.side_effect = RuntimeError("slack down")
        with self._patched(mock_slack) as (_, mock_log):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_log.assert_called_once()
        self.assertIsInstance(mock_log.call_args.kwargs["exception"], RuntimeError)

    def test_posts_fresh_dm_when_stored_message_no_longer_exists(self):
        """When chat_update fails with message_not_found, a fresh DM is posted with the decision and its reference stored."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_update.side_effect = SlackApiError(
            "message_not_found", {"ok": False, "error": "message_not_found"}
        )
        mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "channel": "D0FSC0001", "ts": "2.2"}
        with self._patched(mock_slack) as (mock_store, mock_log):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_called_once()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        self.assertIn("Leave Request Approved", kwargs["blocks"][0]["text"]["text"])
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="2.2")
        # The missing message is recorded, but not as an exception.
        mock_log.assert_called_once()
        self.assertNotIn("exception", mock_log.call_args.kwargs)

    def test_refuses_to_update_a_non_dm_channel_and_posts_fresh_dm(self):
        """A stored reference that is not a direct-message channel is never edited; the decision goes out as a fresh DM instead."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "channel": "D0FSC0001", "ts": "3.3"}
        with self._patched(mock_slack, message_ref=(TEST_SLACK_CHANNEL_ID, "1700000000.000001")) as (
            mock_store,
            mock_log,
        ):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["channel"], TEST_SLACK_USER_ID)
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="3.3")
        mock_log.assert_called_once()

    def test_refuses_to_update_the_attendance_summary_message(self):
        """A stored ts equal to Slack Settings.last_attendance_msg_ts is never edited, even in a DM-looking channel; a fresh DM is posted."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "channel": "D0FSC0001", "ts": "4.4"}

        def get_single_value(doctype, fieldname):
            return "1700000000.000001" if fieldname == "last_attendance_msg_ts" else 0

        with (
            self._patched(mock_slack, message_ref=("D0FSC0001", "1700000000.000001")) as (mock_store, mock_log),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", side_effect=get_single_value),
        ):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="4.4")
        mock_log.assert_called_once()

    def test_other_slack_api_errors_do_not_fall_back_to_a_fresh_dm(self):
        """A chat_update SlackApiError other than a missing message/channel is logged as an error without posting a fresh DM."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        mock_slack.slack_app.client.chat_update.side_effect = SlackApiError(
            "ratelimited", {"ok": False, "error": "ratelimited"}
        )
        with self._patched(mock_slack) as (mock_store, mock_log):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_store.assert_not_called()
        mock_log.assert_called_once()
        self.assertIsInstance(mock_log.call_args.kwargs["exception"], SlackApiError)

    def test_discards_job_when_leave_status_moved_on(self):
        """A queued Approved job finds the leave already Cancelled in the database and does nothing: the Cancelled job renders the latest status."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, current_status="Cancelled") as (mock_store, _):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_store.assert_not_called()

    def test_discards_job_when_leave_was_deleted(self):
        """A job whose leave no longer exists does nothing."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, current_status=None):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_status_check_and_update_happen_under_the_per_leave_lock(self):
        """The status re-read, the reference read and the Slack edit all happen while the same per-leave lock as the submission job is held."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        order = []
        lock = MagicMock()
        lock.__enter__ = MagicMock(side_effect=lambda *a: order.append("lock"))
        lock.__exit__ = MagicMock(side_effect=lambda *a: order.append("unlock"))
        mock_slack.slack_app.client.chat_update.side_effect = lambda **k: order.append("update")
        with (
            self._patched(mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}._applicant_dm_lock", return_value=lock) as mock_lock,
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_leave_status",
                side_effect=lambda name: order.append("status") or "Approved",
            ),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref",
                side_effect=lambda name: order.append("ref") or ("D0FSC0001", "1700000000.000001"),
            ),
        ):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        mock_lock.assert_called_once_with(doc.name)
        self.assertEqual(order, ["lock", "status", "ref", "update", "unlock"])

    def test_lock_name_is_per_leave(self):
        """Each leave gets its own lock so unrelated leaves never wait on each other."""
        with patch(f"{LEAVE_OVERRIDE_MODULE}.filelock") as mock_filelock:
            _applicant_dm_lock("HR-LAP-0050")
            _applicant_dm_lock("HR-LAP-0051")
        names = [call.args[0] for call in mock_filelock.call_args_list]
        self.assertEqual(names, ["fsc_applicant_dm_HR-LAP-0050", "fsc_applicant_dm_HR-LAP-0051"])
        for call in mock_filelock.call_args_list:
            self.assertEqual(call.kwargs["timeout"], APPLICANT_DM_LOCK_TIMEOUT)

    def test_escapes_mrkdwn_control_characters_in_full_name(self):
        """A decider's full name containing &, < or > is escaped before being placed in the mrkdwn status line."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup(approver_slack_id=None)
        with (
            self._patched(mock_slack, mention_user=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value="Tom & Jerry <HR>"),
        ):
            send_leave_decision_to_applicant(doc=doc, status="Approved", decided_by="approver@x.com")
        status_line = mock_slack.slack_app.client.chat_update.call_args.kwargs["blocks"][-1]["text"]["text"]
        self.assertIn("Approved by Tom &amp; Jerry &lt;HR&gt;", status_line)

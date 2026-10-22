from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.override.leave_application import (
    after_insert,
    format_leave_application_blocks,
    on_trash_refresh_attendance_summary,
    on_update_refresh_attendance_summary,
    send_leave_notification_bg,
    send_leave_notification_to_applicant,
    update_attendance_summary,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, TEST_SLACK_USER_ID

LEAVE_OVERRIDE_MODULE = "frappe_slack_connector.override.leave_application"
REFRESH_HANDLER_PATH = f"{LEAVE_OVERRIDE_MODULE}.on_update_refresh_attendance_summary"


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
    first_half_second_half=None,
    total_leave_days=3.0,
    leave_balance=10.0,
    creation="2026-06-09 09:00:00",
    status="Open",
    status_changed=True,
    previous_status="Open",
    docstatus=0,
    docstatus_changed=False,
):
    """Build a MagicMock that mimics a Leave Application doc with the fields the override code reads.

    status_changed / docstatus_changed drive doc.has_value_changed and previous_status
    drives doc.get_value_before_save("status"); the refresh handler gates on these.
    """
    doc = MagicMock()
    doc.name = name
    doc.status = status
    doc.docstatus = docstatus
    changed = {"status": status_changed, "docstatus": docstatus_changed}
    doc.has_value_changed.side_effect = lambda fieldname: changed.get(fieldname, False)
    doc.get_value_before_save.side_effect = lambda fieldname: previous_status if fieldname == "status" else None
    doc.employee = employee
    doc.employee_name = employee_name
    doc.leave_approver = leave_approver
    doc.leave_type = leave_type
    doc.from_date = from_date
    doc.to_date = to_date
    doc.description = description
    doc.half_day = half_day
    doc.half_day_date = half_day_date
    doc.custom_first_halfsecond_half = first_half_second_half
    doc.total_leave_days = total_leave_days
    doc.leave_balance = leave_balance
    doc.creation = creation
    return doc


def _blocks_text(blocks):
    """Concatenate every mrkdwn/plain_text string found in a list of Slack blocks."""
    parts = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text["text"])
        for field in block.get("fields", []):
            parts.append(field["text"])
        for element in block.get("elements", []):
            if isinstance(element.get("text"), str):
                parts.append(element["text"])
    return "\n".join(parts)


def _run_leave_notification_bg(doc, *, custom_fields=False, is_lwp=0, attendance_updates=0, balance_on=10.0):
    """Run send_leave_notification_bg with Slack, DB, dates and HRMS mocked (today is 2026-06-10) and return the mocks.

    `balance_on` configures the patched get_leave_balance_on: a float is returned, an Exception instance is raised.
    """
    mock_slack = MagicMock()
    mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
    # Resolve by lookup kind rather than call order so extra lookups do not shift the answers
    mock_slack.get_slack_user_id.side_effect = lambda *args, **kwargs: (
        "U-applicant" if "employee_id" in kwargs else "U-approver"
    )
    settings = _build_slack_settings_mock(send_attendance_updates=attendance_updates, last_attendance_date="2026-06-10")
    balance_kwargs = {"side_effect": balance_on} if isinstance(balance_on, Exception) else {"return_value": balance_on}

    # Answer only the lookups the job makes: Employee status (always Active) and Leave Type.is_lwp
    def fake_get_value(doctype, *args, **kwargs):
        return "Active" if doctype == "Employee" else is_lwp

    with (
        patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", side_effect=fake_get_value) as mock_get_value,
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.set_value"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today", return_value="2026-06-10"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate", return_value="2026-06-10"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=custom_fields),
        patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", **balance_kwargs) as mock_balance_on,
        patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_error_log,
    ):
        send_leave_notification_bg(doc)
    leave_type_lookups = [c.args for c in mock_get_value.call_args_list if c.args[0] == "Leave Type"]
    assert leave_type_lookups in ([], [("Leave Type", doc.leave_type, "is_lwp")])
    return {
        "slack": mock_slack,
        "get_value": mock_get_value,
        "get_leave_balance_on": mock_balance_on,
        "generate_error_log": mock_error_log,
    }


def _approver_call(mock_slack):
    """Return the chat_postMessage call that went to the approver DM."""
    return next(
        c for c in mock_slack.slack_app.client.chat_postMessage.call_args_list if c.kwargs["channel"] == "U-approver"
    )


def _get_approver_blocks(doc, *, custom_fields=False, is_lwp=0, balance_on=10.0):
    """Run send_leave_notification_bg (attendance updates off) and return the blocks posted to the approver DM."""
    mocks = _run_leave_notification_bg(doc, custom_fields=custom_fields, is_lwp=is_lwp, balance_on=balance_on)
    mocks["slack"].slack_app.client.chat_postMessage.assert_called_once()
    return _approver_call(mocks["slack"]).kwargs["blocks"]


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
            # After commit: the balance lookup authorises through the committed Leave Application row
            self.assertTrue(call.kwargs["enqueue_after_commit"])

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

    def test_does_nothing_when_rejected_leave_is_cancelled(self):
        """A Rejected -> Cancelled transition was never in the summary, so it does not enqueue a refresh (and does not read Slack Settings)."""
        doc = _build_leave_doc(
            from_date="2026-06-10", to_date="2026-06-10", status="Cancelled", previous_status="Rejected"
        )
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings) as mock_get_single,
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            on_update_refresh_attendance_summary(doc, method="on_cancel")
        mock_enqueue.assert_not_called()
        mock_get_single.assert_not_called()

    def test_enqueues_refresh_when_approved_leave_is_force_cancelled(self):
        """A cancel with ignore_validate skips HRMS before_cancel, so the status stays Approved while docstatus becomes 2; the on_cancel hook must still refresh because the summary query drops the row on docstatus."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-10",
            status="Approved",
            status_changed=False,
            previous_status="Approved",
            docstatus=2,
            docstatus_changed=True,
        )
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            on_update_refresh_attendance_summary(doc, method="on_cancel")
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], update_attendance_summary)

    def test_does_nothing_when_rejected_leave_is_force_cancelled(self):
        """Force-cancelling a leave that was already Rejected does not refresh: it was never in the summary."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-10",
            status="Rejected",
            status_changed=False,
            previous_status="Rejected",
            docstatus=2,
            docstatus_changed=True,
        )
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_refresh_attendance_summary(doc, method="on_cancel")
        mock_enqueue.assert_not_called()

    def test_submitted_approved_leave_does_not_refresh_on_update_after_submit(self):
        """An update-after-submit on an Approved leave (docstatus 1, unchanged) is not a cancel and enqueues nothing."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-10",
            status="Approved",
            status_changed=False,
            previous_status="Approved",
            docstatus=1,
        )
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_refresh_attendance_summary(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_real_force_cancelled_leave_application_doc_enqueues_refresh(self):
        """With a real Leave Application whose before-save copy is Approved at docstatus 1 and which is now docstatus 2 with the status untouched, has_value_changed("docstatus") resolves naturally and the handler enqueues the refresh."""
        fields = {
            "doctype": "Leave Application",
            "employee": "EMP-001",
            "employee_name": "Alice",
            "leave_type": "Casual Leave",
            "from_date": "2026-06-10",
            "to_date": "2026-06-10",
            "posting_date": "2026-06-09",
            "status": "Approved",
        }
        doc = frappe.get_doc({**fields, "docstatus": 2})
        doc._doc_before_save = frappe.get_doc({**fields, "docstatus": 1})
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            on_update_refresh_attendance_summary(doc, method="on_cancel")
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], update_attendance_summary)

    def test_enqueues_refresh_for_approved_to_cancelled_and_open_to_rejected(self):
        """Transitions out of a counted status (Open or Approved) into Rejected or Cancelled enqueue the refresh."""
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        for previous_status, status in (("Approved", "Cancelled"), ("Open", "Rejected"), ("Approved", "Rejected")):
            doc = _build_leave_doc(
                from_date="2026-06-10", to_date="2026-06-10", status=status, previous_status=previous_status
            )
            mock_enqueue = self._run(doc, settings)
            mock_enqueue.assert_called_once()

    def test_enqueues_refresh_when_previous_status_unknown(self):
        """When there is no before-save copy (get_value_before_save returns None) the handler refreshes rather than risk a stale summary."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected", previous_status=None)
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_called_once()

    def test_enqueues_refresh_on_real_leave_application_doc(self):
        """With a real (uninserted) Leave Application whose _doc_before_save copy is Open and whose status is Rejected, has_value_changed/get_value_before_save resolve naturally and the handler enqueues the refresh."""
        fields = {
            "doctype": "Leave Application",
            "employee": "EMP-001",
            "employee_name": "Alice",
            "leave_type": "Casual Leave",
            "from_date": "2026-06-10",
            "to_date": "2026-06-10",
            "posting_date": "2026-06-09",
        }
        doc = frappe.get_doc({**fields, "status": "Rejected"})
        doc._doc_before_save = frappe.get_doc({**fields, "status": "Open"})
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], update_attendance_summary)

    def test_real_leave_application_doc_with_unchanged_status_does_nothing(self):
        """With a real Leave Application whose before-save copy already has status Rejected, saving again does not enqueue the refresh."""
        fields = {
            "doctype": "Leave Application",
            "employee": "EMP-001",
            "employee_name": "Alice",
            "leave_type": "Casual Leave",
            "from_date": "2026-06-10",
            "to_date": "2026-06-10",
            "posting_date": "2026-06-09",
            "status": "Rejected",
        }
        doc = frappe.get_doc(dict(fields))
        doc._doc_before_save = frappe.get_doc(dict(fields))
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        mock_enqueue = self._run(doc, settings)
        mock_enqueue.assert_not_called()


class TestOnTrashRefreshAttendanceSummary(IntegrationTestCase):
    def _run(self, doc):
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-10")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{LEAVE_OVERRIDE_MODULE}.today", return_value="2026-06-10"),
        ):
            on_trash_refresh_attendance_summary(doc, method="on_trash")
        return mock_enqueue

    def test_deleting_a_counted_draft_covering_today_refreshes_the_summary(self):
        """Deleting an Open draft that covers today (it was added to the summary when applied for) enqueues the refresh after commit; a delete runs neither on_update nor on_cancel."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Open", status_changed=False)
        mock_enqueue = self._run(doc)
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], update_attendance_summary)
        self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])

    def test_deleting_a_rejected_or_cancelled_leave_does_nothing(self):
        """A leave the summary already excludes (Rejected, or docstatus 2) does not refresh it when deleted."""
        rejected = _build_leave_doc(
            from_date="2026-06-10", to_date="2026-06-10", status="Rejected", status_changed=False
        )
        self._run(rejected).assert_not_called()
        cancelled = _build_leave_doc(
            from_date="2026-06-10", to_date="2026-06-10", status="Approved", status_changed=False, docstatus=2
        )
        self._run(cancelled).assert_not_called()

    def test_deleting_a_leave_not_covering_today_does_nothing(self):
        doc = _build_leave_doc(from_date="2026-06-12", to_date="2026-06-12", status="Open", status_changed=False)
        self._run(doc).assert_not_called()


class TestDocEventHooks(IntegrationTestCase):
    def test_refresh_handler_wired_to_update_cancel_discard_and_trash(self):
        """hooks.py registers on_update_refresh_attendance_summary for Leave Application on_update, on_cancel and on_discard, and on_trash_refresh_attendance_summary for on_trash (get_hooks returns a list per event)."""
        leave_events = frappe.get_hooks("doc_events")["Leave Application"]
        for event in ("on_update", "on_cancel", "on_discard"):
            self.assertIn(REFRESH_HANDLER_PATH, leave_events[event], event)
        self.assertIn(f"{LEAVE_OVERRIDE_MODULE}.on_trash_refresh_attendance_summary", leave_events["on_trash"])
        self.assertIn(f"{LEAVE_OVERRIDE_MODULE}.after_insert", leave_events["after_insert"])


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
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
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

    def test_approver_dm_shows_requested_days_and_balance_before_request(self):
        """The approver DM shows the requested days and the balance before the request for a leave type with an allocation."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        text = _blocks_text(_get_approver_blocks(doc, balance_on=10.0))
        self.assertIn("*Requested:*\n3 day(s)", text)
        self.assertIn("*Balance before this request:*\n10 day(s)", text)

    def test_approver_dm_warns_when_requested_days_exceed_balance(self):
        """When total_leave_days exceeds the balance HRMS reports, the approver DM carries the insufficient-balance warning with the shortfall and resulting balance."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        text = _blocks_text(_get_approver_blocks(doc, balance_on=1.0))
        self.assertIn(
            ":warning: *Insufficient balance:* this request exceeds the available balance by 2 day(s). "
            "Approving it will take the balance to -2.",
            text,
        )

    def test_approver_dm_has_no_warning_when_balance_is_sufficient(self):
        """No insufficient-balance warning is added when the balance covers total_leave_days (including when they are equal)."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        text = _blocks_text(_get_approver_blocks(doc, balance_on=3.0))
        self.assertNotIn("Insufficient balance", text)
        self.assertIn("*Balance before this request:*\n3 day(s)", text)

    def test_approver_dm_skips_balance_and_warning_for_leave_without_pay(self):
        """For a Leave Type with is_lwp set, the approver DM shows neither the balance nor the warning, even when the balance is lower than the request."""
        doc = _build_leave_doc(
            from_date="2026-06-15",
            to_date="2026-06-17",
            leave_type="Leave Without Pay",
            total_leave_days=3.0,
            leave_balance=0.0,
        )
        text = _blocks_text(_get_approver_blocks(doc, is_lwp=1))
        self.assertNotIn("Balance before this request", text)
        self.assertNotIn("Insufficient balance", text)
        self.assertIn("*Requested:*\n3 day(s)", text)

    def test_approver_dm_shows_half_for_future_single_day_half_day_leave(self):
        """A future single-day half-day leave is shown with its half (custom first/second half field) rather than as Full Day."""
        doc = _build_leave_doc(
            from_date="2026-06-15",
            to_date="2026-06-15",
            half_day=1,
            half_day_date="2026-06-15",
            first_half_second_half="Second Half",
            total_leave_days=0.5,
        )
        text = _blocks_text(_get_approver_blocks(doc, custom_fields=True))
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Second Half", text)
        self.assertNotIn("Full Day", text)

    def test_approver_dm_shows_half_day_for_future_single_day_leave_without_custom_fields(self):
        """Without the custom half fields, a future single-day half-day leave is labelled Half Day."""
        doc = _build_leave_doc(
            from_date="2026-06-15",
            to_date="2026-06-15",
            half_day=1,
            half_day_date="2026-06-15",
            total_leave_days=0.5,
        )
        text = _blocks_text(_get_approver_blocks(doc, custom_fields=False))
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Half Day", text)

    def test_approver_dm_shows_half_day_date_for_multi_day_leave_with_one_half_day(self):
        """A multi-day leave with one half day shows the half-day date and its half, not Half Day for the whole leave."""
        doc = _build_leave_doc(
            from_date="2026-06-15",
            to_date="2026-06-17",
            half_day=1,
            half_day_date="2026-06-16",
            first_half_second_half="First Half",
            total_leave_days=2.5,
        )
        text = _blocks_text(_get_approver_blocks(doc, custom_fields=True))
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Half day on Jun 16, 2026 (Tue) — First Half", text)
        self.assertIn("*Requested:*\n2.5 day(s)", text)
        self.assertNotIn("*Duration:*\n:hourglass_flowing_sand: Half Day", text)
        self.assertNotIn("*Duration:*\n:hourglass_flowing_sand: First Half\n", text)

    def test_approver_dm_shows_half_day_date_without_period_when_custom_fields_missing(self):
        """Without the custom half fields, a multi-day leave with one half day shows only the half-day date."""
        doc = _build_leave_doc(
            from_date="2026-06-15",
            to_date="2026-06-17",
            half_day=1,
            half_day_date="2026-06-16",
            total_leave_days=2.5,
        )
        text = _blocks_text(_get_approver_blocks(doc, custom_fields=False))
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Half day on Jun 16, 2026 (Tue)\n", text)

    def test_approver_dm_computes_balance_even_when_doc_carries_zero(self):
        """leave_balance is a NOT NULL column, so a server-created leave reads back as 0.0 after any reload before the job runs (a Notification on save reloads the doc): the balance must still come from HRMS, not show 0 with a false warning."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-16", total_leave_days=2.0, leave_balance=0.0)
        mocks = _run_leave_notification_bg(doc, balance_on=12.0)
        mocks["get_leave_balance_on"].assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn("*Balance before this request:*\n12 day(s)", text)
        self.assertNotIn("Insufficient balance", text)

    def test_approver_dm_shows_a_real_zero_balance_with_the_warning(self):
        """When HRMS really reports 0, the DM shows 0 day(s) and the insufficient-balance warning."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-16", total_leave_days=2.0, leave_balance=0.0)
        mocks = _run_leave_notification_bg(doc, balance_on=0.0)
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn("*Balance before this request:*\n0 day(s)", text)
        self.assertIn("exceeds the available balance by 2 day(s). Approving it will take the balance to -2.", text)

    def test_approver_dm_computes_balance_when_not_stored_on_doc(self):
        """When doc.leave_balance is None (application created on the server), the balance is computed via HRMS get_leave_balance_on with the Desk form's arguments and shown."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=None)
        mocks = _run_leave_notification_bg(doc, balance_on=7.0)
        mocks["get_leave_balance_on"].assert_called_once_with(
            "EMP-001",
            "Casual Leave",
            "2026-06-15",
            "2026-06-17",
            consider_all_leaves_in_the_allocation_period=True,
            leave_application=doc.name,
        )
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn("*Balance before this request:*\n7 day(s)", text)
        self.assertNotIn("Insufficient balance", text)
        mocks["generate_error_log"].assert_not_called()

    def test_approver_dm_falls_back_to_the_stored_balance_when_the_lookup_fails(self):
        """When get_leave_balance_on raises but the form stored a balance, that value is shown (and the failure logged)."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=10.0)
        mocks = _run_leave_notification_bg(doc, balance_on=PermissionError("no leave access"))
        mocks["generate_error_log"].assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn("*Balance before this request:*\n10 day(s)", text)

    def test_approver_dm_omits_balance_and_logs_when_balance_lookup_fails(self):
        """When get_leave_balance_on raises and nothing usable is stored, the error is logged, the balance block and warning are omitted, and the approver DM is still posted."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=None)
        mocks = _run_leave_notification_bg(doc, balance_on=PermissionError("no leave access"))
        mocks["generate_error_log"].assert_called_once()
        self.assertEqual(mocks["generate_error_log"].call_args.kwargs["title"], "Error fetching leave balance")
        mocks["slack"].slack_app.client.chat_postMessage.assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertNotIn("Balance before this request", text)
        self.assertNotIn("Insufficient balance", text)
        self.assertIn("*Requested:*\n3 day(s)", text)

    def test_approver_dm_always_computes_balance_except_for_lwp(self):
        """get_leave_balance_on is called even when the doc carries leave_balance (the stored value can be stale or a NOT NULL 0), and never for a Leave Without Pay type."""
        stored = _run_leave_notification_bg(_build_leave_doc(leave_balance=10.0), balance_on=9.0)
        stored["get_leave_balance_on"].assert_called_once()
        self.assertIn(
            "*Balance before this request:*\n9 day(s)", _blocks_text(_approver_call(stored["slack"]).kwargs["blocks"])
        )
        lwp = _run_leave_notification_bg(_build_leave_doc(leave_type="Leave Without Pay", leave_balance=None), is_lwp=1)
        lwp["get_leave_balance_on"].assert_not_called()

    def test_approver_dm_carries_a_text_fallback(self):
        """The approver DM has a text= fallback for notification previews, naming the employee with Slack's control characters escaped."""
        mocks = _run_leave_notification_bg(_build_leave_doc(employee_name="Alice <A&B>"), balance_on=10.0)
        self.assertEqual(
            _approver_call(mocks["slack"]).kwargs["text"], "New leave application from Alice &lt;A&amp;B&gt;"
        )

    def test_approver_dm_adds_the_request_back_for_a_leave_inserted_approved_and_submitted(self):
        """A leave inserted already approved and submitted has its ledger entry when the job runs, so HRMS reports the balance after the deduction; the DM adds the requested days back to show the balance before the request."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        doc.docstatus = 1
        doc.status = "Approved"
        text = _blocks_text(_get_approver_blocks(doc, balance_on=7.0))
        self.assertIn("*Balance before this request:*\n10 day(s)", text)
        self.assertNotIn("Insufficient balance", text)

    def test_approver_dm_does_not_add_the_request_back_for_a_submitted_rejected_leave(self):
        """A rejected leave is submitted without a ledger entry, so nothing was deducted and nothing is added back."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        doc.docstatus = 1
        doc.status = "Rejected"
        text = _blocks_text(_get_approver_blocks(doc, balance_on=7.0))
        self.assertIn("*Balance before this request:*\n7 day(s)", text)

    def test_attendance_thread_keeps_today_period_for_multi_day_half_day_leave_starting_today(self):
        """For a multi-day leave starting today with the half day today, the attendance thread says _(First Half)_ while the approver DM carries the full duration text."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-12",
            half_day=1,
            half_day_date="2026-06-10",
            first_half_second_half="First Half",
            total_leave_days=2.5,
        )
        mocks = _run_leave_notification_bg(doc, custom_fields=True, attendance_updates=1)
        calls = mocks["slack"].slack_app.client.chat_postMessage.call_args_list
        self.assertEqual(len(calls), 2)
        thread_call = next(c for c in calls if c.kwargs.get("thread_ts") == "1700000000.000001")
        self.assertIn("requested for leave today. _(First Half)_", thread_call.kwargs["blocks"][0]["text"]["text"])
        approver_text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(
            "*Duration:*\n:hourglass_flowing_sand: Half day on Jun 10, 2026 (Wed) — First Half", approver_text
        )

    def test_attendance_thread_says_full_day_when_half_day_date_is_missing(self):
        """A half_day leave starting today with no half_day_date is reported as _(Full Day)_ in the thread instead of resolving the missing date to today."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-12",
            half_day=1,
            half_day_date=None,
            first_half_second_half="First Half",
            total_leave_days=2.5,
        )
        mocks = _run_leave_notification_bg(doc, custom_fields=True, attendance_updates=1)
        thread_call = next(
            c
            for c in mocks["slack"].slack_app.client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1700000000.000001"
        )
        self.assertIn("requested for leave today. _(Full Day)_", thread_call.kwargs["blocks"][0]["text"]["text"])

    def test_attendance_thread_says_full_day_when_half_day_falls_on_another_date(self):
        """For a multi-day leave starting today whose half day is on a later date, the attendance thread says _(Full Day)_ for today."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-12",
            half_day=1,
            half_day_date="2026-06-12",
            first_half_second_half="Second Half",
            total_leave_days=2.5,
        )
        mocks = _run_leave_notification_bg(doc, custom_fields=True, attendance_updates=1)
        thread_call = next(
            c
            for c in mocks["slack"].slack_app.client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1700000000.000001"
        )
        self.assertIn("requested for leave today. _(Full Day)_", thread_call.kwargs["blocks"][0]["text"]["text"])
        approver_text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(
            "*Duration:*\n:hourglass_flowing_sand: Half day on Jun 12, 2026 (Fri) — Second Half", approver_text
        )

    def test_approver_dm_shows_full_day_for_leave_without_half_day(self):
        """A leave with half_day unset is labelled Full Day."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", half_day=0)
        text = _blocks_text(_get_approver_blocks(doc))
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Full Day", text)


class TestFormatLeaveApplicationBlocks(IntegrationTestCase):
    def _default_kwargs(self, **overrides):
        kwargs = {
            "leave_id": "HR-LAP-0050",
            "leave_link": "https://erp.example.com/app/leave-application/HR-LAP-0050",
            "employee_name": "<@U-applicant>",
            "leave_type": "Casual Leave",
            "duration": "Full Day",
            "leave_submission_date": "Jun 09, 2026 (Tue)",
            "from_date": "Jun 15, 2026 (Mon)",
            "to_date": "Jun 17, 2026 (Wed)",
            "reason": "vacation",
            "total_days": 3.0,
            "leave_balance": 10.0,
        }
        kwargs.update(overrides)
        return kwargs

    def test_default_case_block_structure(self):
        """The default (sufficient balance) message has header, leave id, type/submitted, from/to, duration/requested/balance fields, reason, approve/reject actions and no warning."""
        blocks = format_leave_application_blocks(**self._default_kwargs())
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("New Leave Application", blocks[0]["text"]["text"])
        self.assertIn("<@U-applicant> has submitted a new leave request.", blocks[1]["text"]["text"])
        self.assertIn("HR-LAP-0050", blocks[2]["elements"][0]["text"])

        field_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        field_texts = [f["text"] for section in field_sections for f in section["fields"]]
        self.assertIn("*Leave Type:*\n:rocket: Casual Leave", field_texts)
        self.assertIn("*Submitted On:*\n:clock3: Jun 09, 2026 (Tue)", field_texts)
        self.assertIn("*From:*\n:date: Jun 15, 2026 (Mon)", field_texts)
        self.assertIn("*To:*\n:date: Jun 17, 2026 (Wed)", field_texts)
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Full Day", field_texts)
        self.assertIn("*Requested:*\n3 day(s)", field_texts)
        self.assertIn("*Balance before this request:*\n10 day(s)", field_texts)

        text = _blocks_text(blocks)
        self.assertIn("*Reason:*\n>vacation", text)
        self.assertNotIn("Insufficient balance", text)
        self.assertNotIn("Half Day:", text)

        actions = next(b for b in blocks if b.get("type") == "actions")
        self.assertEqual(actions["block_id"], "leave_actions_block")
        self.assertEqual([e["action_id"] for e in actions["elements"]], ["leave_approve", "leave_reject"])
        self.assertTrue(all(e["value"] == "HR-LAP-0050" for e in actions["elements"]))

    def test_omits_requested_and_balance_fields_when_not_provided(self):
        """When total_days and leave_balance are None, only the duration field is added and no warning appears."""
        blocks = format_leave_application_blocks(**self._default_kwargs(total_days=None, leave_balance=None))
        text = _blocks_text(blocks)
        self.assertIn("*Duration:*\n:hourglass_flowing_sand: Full Day", text)
        self.assertNotIn("*Requested:*", text)
        self.assertNotIn("Balance before this request", text)
        self.assertNotIn("Insufficient balance", text)

    def test_warning_uses_g_number_formatting(self):
        """Shortfall and resulting balance are formatted with :g so fractional halves keep their decimals and whole numbers drop .0."""
        blocks = format_leave_application_blocks(**self._default_kwargs(total_days=2.5, leave_balance=1.0))
        text = _blocks_text(blocks)
        self.assertIn("exceeds the available balance by 1.5 day(s). Approving it will take the balance to -1.5.", text)

    def test_rounds_before_comparing_so_float_noise_does_not_trigger_warning(self):
        """total_days and leave_balance are rounded to 2 places before the comparison, so 3.0000001 vs 3 shows no warning."""
        blocks = format_leave_application_blocks(**self._default_kwargs(total_days=3.0000001, leave_balance=3.0))
        text = _blocks_text(blocks)
        self.assertNotIn("Insufficient balance", text)
        self.assertIn("*Requested:*\n3 day(s)", text)


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

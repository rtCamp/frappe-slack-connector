import contextlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.db.leave_application import APPLICANT_CHANNEL_FIELD, APPLICANT_MSG_TS_FIELD
from frappe_slack_connector.override.leave_application import (
    APPLICANT_DM_LOCK_TIMEOUT,
    ATTENDANCE_REPLY_CHANNEL_FIELD,
    ATTENDANCE_REPLY_FIELDS,
    ATTENDANCE_REPLY_TEXT_FIELD,
    ATTENDANCE_REPLY_TS_FIELD,
    _applicant_dm_lock,
    _format_decider,
    after_insert,
    format_leave_application_blocks,
    format_leave_submission_blocks,
    on_discard_notify_applicant,
    on_trash_notify_applicant,
    on_trash_refresh_attendance_summary,
    on_trash_withdraw_attendance_reply,
    on_update_notify_applicant,
    on_update_refresh_attendance_summary,
    on_update_withdraw_attendance_reply,
    post_same_day_leave_to_attendance_thread,
    reason_blocks,
    restore_applicant_message_ref,
    restore_attendance_reply_ts,
    send_leave_decision_to_applicant,
    send_leave_deleted_to_applicant,
    send_leave_notification_bg,
    send_leave_notification_to_applicant,
    update_attendance_summary,
    withdraw_attendance_reply_bg,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, TEST_SLACK_USER_ID

LEAVE_OVERRIDE_MODULE = "frappe_slack_connector.override.leave_application"
ATTENDANCE_MODULE = "frappe_slack_connector.tasks.attendance_summary"

TODAY = "2026-06-10"
SUMMARY_TS = "1700000000.000001"
REPLY_TS = "1700000500.000001"
# Channel ID Slack reports for the reply, distinct from the configured channel so a test can tell them apart
REPLY_CHANNEL = "C0FSCREPLY"


def _slack_ts_at_noon_utc(day: str) -> str:
    """Return a Slack-style ``ts`` for 12:00 UTC on ``day`` (YYYY-MM-DD).

    Noon UTC lands on the same calendar day in every site timezone between
    UTC-11 and UTC+11, so the date derived from the ts is stable in tests.
    """
    moment = datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)
    return f"{moment.timestamp():.6f}"


TODAY_TS = _slack_ts_at_noon_utc(TODAY)
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
    attendance_reply_ts=None,
    docstatus=0,
    docstatus_changed=False,
    in_insert=False,
    modified_by="approver@x.com",
    modified="2026-06-10 10:00:00",
):
    """Build a MagicMock that mimics a Leave Application doc with the fields the override code reads.

    status_changed drives doc.has_value_changed("status") and previous_status drives
    doc.get_value_before_save("status"); the refresh handler gates on both.

    ``attendance_reply_ts`` is the value carried on the doc itself; the removal code must ignore it
    and read the database instead (see ``_leave_override_env(stored_ts=...)``).
    """
    doc = MagicMock()
    doc.name = name
    doc.status = status
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
    doc.status = status
    doc.custom_slack_attendance_reply_ts = attendance_reply_ts
    # No before-save copy unless a test sets one: the handlers then read the stored reference from the database
    doc.get_doc_before_save.return_value = None
    doc.get.side_effect = lambda fieldname, default=None: getattr(doc, fieldname, default)
    doc.docstatus = docstatus
    doc.modified_by = modified_by
    doc.modified = modified
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
            # rich_text block -> rich_text_quote -> text elements
            for inner in element.get("elements", []):
                if isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
    return "\n".join(parts)


def _reason_quote(blocks):
    """The literal text element of the reason quote, with its style."""
    rich = next(b for b in blocks if b.get("type") == "rich_text")
    return rich["elements"][0]["elements"][0]


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

    # The same-day thread reply checks the Employee status; everything else asks for Leave Type.is_lwp
    def fake_get_value(doctype, *args, **kwargs):
        if doctype == "Employee":
            return "Active"
        if args and isinstance(args[-1], list) and "status" in args[-1]:
            # the committed row the thread post loads under its lock: the test's doc, announced, without a reply
            return frappe._dict(
                name=doc.name,
                status="Open",
                docstatus=0,
                from_date=doc.from_date,
                to_date=doc.to_date,
                employee=doc.employee,
                employee_name=doc.employee_name,
                half_day=doc.half_day,
                half_day_date=doc.half_day_date,
                custom_first_halfsecond_half=getattr(doc, "custom_first_halfsecond_half", None),
            )
        return is_lwp

    with (
        patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", side_effect=fake_get_value) as mock_get_value,
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.set_value"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.get_employees_on_holiday", return_value=set()),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today", return_value="2026-06-10"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.nowdate", return_value="2026-06-10"),
        patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=custom_fields),
        # The thread reply's day period comes from the attendance summary helper, which has its own import
        patch("frappe_slack_connector.tasks.attendance_summary.custom_fields_exist", return_value=custom_fields),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit"),
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
    last_attendance_msg_ts=SUMMARY_TS,
    mention_user=1,
):
    """Build a MagicMock that mimics Slack Settings Single doc."""
    settings = MagicMock()
    settings.send_attendance_updates = send_attendance_updates
    settings.last_attendance_date = last_attendance_date
    settings.last_attendance_msg_ts = last_attendance_msg_ts
    settings.last_attendance_channel_id = None
    settings.mention_user = mention_user
    # Document.get(): a field the doctype does not have yet reads as None, never as a MagicMock
    settings.get.side_effect = lambda fieldname, default=None: getattr(settings, fieldname, default)
    return settings


def _build_slack_mock(*, applicant_slack_id="U-applicant", approver_slack_id="U-approver"):
    """Build a SlackIntegration MagicMock whose user lookups resolve by kwarg and whose thread reply returns REPLY_TS."""
    mock_slack = MagicMock()
    mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID

    def lookup(*args, **kwargs):
        if "user_email" in kwargs:
            return approver_slack_id
        return applicant_slack_id

    mock_slack.get_slack_user_id.side_effect = lookup
    mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "ts": REPLY_TS, "channel": REPLY_CHANNEL}
    mock_slack.slack_app.client.chat_update.return_value = {"ok": True}
    return mock_slack


@contextlib.contextmanager
def _leave_override_env(
    *,
    settings=None,
    employee_status="Active",
    employee_on_holiday=False,
    today=TODAY,
    stored_ts=None,
    stored_channel=REPLY_CHANNEL,
    stored_text=None,
    db_states=None,
    doc=None,
):
    """Patch the Frappe surface the leave override reads: Slack Settings, today's date, the Employee status lookup, the stored reply reference (ts and channel) and its write-back.

    ``frappe.db.get_value`` / ``get_single_value`` are patched with scoped side effects that answer only the
    lookups the override makes and delegate every other call to the real function, so Frappe internals
    (controller lookup, system settings) keep working. ``generate_error_log`` is patched too, so an
    unexpected exception shows up as an assertion instead of a real Error Log insert.

    Yields a namespace with the ``get_value``, ``set_value`` and ``error_log`` mocks.
    """
    settings = settings if settings is not None else _build_slack_settings_mock()
    states = list(db_states) if db_states else None
    # The committed row the post job loads under its lock mirrors the test's doc (dates, employee, half day)
    row_doc = doc if doc is not None else _build_leave_doc()
    real_get_value = frappe.db.get_value

    def next_state():
        """One scripted state per read, the last one repeating. The default mirrors the test's doc (status and
        docstatus); without a doc it is a withdrawn row, which is what the withdrawal job tests exercise."""
        if states:
            return states.pop(0) if len(states) > 1 else states[0]
        if doc is None:
            return {"status": "Cancelled", "docstatus": 2}
        return {"status": doc.status, "docstatus": doc.docstatus}

    real_get_single_value = frappe.db.get_single_value

    # Same parameter names as frappe.db.get_value so delegated keyword calls bind correctly.
    def fake_get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
        if doctype == "Employee" and fieldname == "status":
            return employee_status
        if doctype == "Leave Application" and fieldname == list(ATTENDANCE_REPLY_FIELDS):
            assert kwargs.get("as_dict"), "the reply reference is read as a dict"
            assert kwargs.get("ignore"), "a missing column (fixtures not yet synced) must not break a save"
            return frappe._dict(
                {
                    ATTENDANCE_REPLY_TS_FIELD: stored_ts,
                    ATTENDANCE_REPLY_CHANNEL_FIELD: stored_channel,
                    ATTENDANCE_REPLY_TEXT_FIELD: stored_text,
                }
            )
        if doctype == "Leave Application" and fieldname == ["status", "docstatus", *ATTENDANCE_REPLY_FIELDS]:
            # The committed row's status and reply reference, re-read under the lock
            assert kwargs.get("as_dict") and kwargs.get("ignore")
            state = next_state()
            if state is None:
                return None
            return frappe._dict(
                {
                    ATTENDANCE_REPLY_TS_FIELD: stored_ts,
                    ATTENDANCE_REPLY_CHANNEL_FIELD: stored_channel,
                    ATTENDANCE_REPLY_TEXT_FIELD: stored_text,
                    **state,
                }
            )
        if (
            doctype == "Leave Application"
            and isinstance(fieldname, list)
            and fieldname[:3] == ["name", "status", "docstatus"]
        ):
            # The whole committed row the post job decides from: the test's doc plus the scripted state
            assert kwargs.get("as_dict") and kwargs.get("ignore")
            state = next_state()
            if state is None:
                return None
            return frappe._dict(
                {
                    "name": row_doc.name,
                    "from_date": row_doc.from_date,
                    "to_date": row_doc.to_date,
                    "employee": row_doc.employee,
                    "employee_name": row_doc.employee_name,
                    "half_day": row_doc.half_day,
                    "half_day_date": row_doc.half_day_date,
                    "custom_first_halfsecond_half": getattr(row_doc, "custom_first_halfsecond_half", None),
                    ATTENDANCE_REPLY_TS_FIELD: stored_ts,
                    ATTENDANCE_REPLY_CHANNEL_FIELD: stored_channel,
                    ATTENDANCE_REPLY_TEXT_FIELD: stored_text,
                    **state,
                }
            )
        return real_get_value(doctype, filters, fieldname, *args, **kwargs)

    def fake_get_single_value(doctype, fieldname, *args, **kwargs):
        if doctype == "Slack Settings" and fieldname == "last_attendance_msg_ts":
            return settings.last_attendance_msg_ts
        if doctype == "Slack Settings" and fieldname == "mention_user":
            return settings.mention_user
        return real_get_single_value(doctype, fieldname, *args, **kwargs)

    with (
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.utils.today", return_value=today),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", side_effect=fake_get_value) as mock_get_value,
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", side_effect=fake_get_single_value),
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.set_value") as mock_set_value,
        patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit") as mock_commit,
        patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_error_log,
        patch(
            f"{LEAVE_OVERRIDE_MODULE}.get_employees_on_holiday",
            side_effect=lambda employees, on_date: set(employees) if employee_on_holiday else set(),
        ) as mock_on_holiday,
    ):
        yield SimpleNamespace(
            get_value=mock_get_value,
            set_value=mock_set_value,
            commit=mock_commit,
            error_log=mock_error_log,
            on_holiday=mock_on_holiday,
        )


@contextlib.contextmanager
def _in_import_flag(value=True):
    """Temporarily set frappe.flags.in_import, restoring the previous value afterwards."""
    original = frappe.flags.in_import
    frappe.flags.in_import = value
    try:
        yield
    finally:
        frappe.flags.in_import = original


def _thread_reply_text(mock_slack) -> str:
    """Return the mrkdwn text of the single attendance-thread reply posted on the mock client."""
    mock_slack.slack_app.client.chat_postMessage.assert_called_once()
    kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
    return kwargs["blocks"][0]["text"]["text"]


WITHDRAW_JOB_KWARGS = {
    "employee": "EMP-001",
    "employee_name": "Alice",
    "day_period": "Full Day",
    "channel": REPLY_CHANNEL,
}


def _assert_struck_through(
    mock_slack, reply_ts, *, name="<@U-applicant>", day_period="Full Day", channel=REPLY_CHANNEL
):
    """Assert the reply was edited in place (not deleted), in the stored channel, into the struck-through text ending in (Cancelled)."""
    label = "Cancelled"
    mock_slack.slack_app.client.chat_delete.assert_not_called()
    mock_slack.slack_app.client.chat_update.assert_called_once()
    kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
    assert kwargs["channel"] == channel, kwargs
    assert kwargs["ts"] == reply_ts, kwargs
    assert kwargs["blocks"][0]["text"]["text"] == f"~{name} is on leave today. _({day_period})_~ ({label})", kwargs
    assert f"({label})" in kwargs["text"], kwargs
    assert "<" not in kwargs["text"].replace("&lt;", ""), kwargs  # fallback text is escaped like the block


def _run_enqueued_inline(method, **kwargs):
    """frappe.enqueue stand-in that runs the job synchronously with the job kwargs it was given."""
    job_kwargs = {k: v for k, v in kwargs.items() if k not in ("queue", "enqueue_after_commit")}
    return method(**job_kwargs)


def _refresh_calls(mock_enqueue):
    """Return the enqueue calls that target update_attendance_summary."""
    return [call for call in mock_enqueue.call_args_list if call.args[0] is update_attendance_summary]


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg (announcing in the thread) and send_leave_notification_to_applicant, both on the short queue and only after the insert commits (the thread job writes its ts back to the row)."""
        doc = _build_leave_doc()
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.get_single", return_value=settings),
        ):
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        calls = {call.args[0]: call.kwargs for call in mock_enqueue.call_args_list}
        self.assertIn(send_leave_notification_bg, calls)
        self.assertIn(send_leave_notification_to_applicant, calls)
        for kwargs in calls.values():
            self.assertEqual(kwargs["queue"], "short")
            self.assertTrue(kwargs["enqueue_after_commit"])
            self.assertIs(kwargs["doc"], doc)
        self.assertTrue(calls[send_leave_notification_bg]["announce_in_thread"])

    def test_does_not_announce_in_thread_during_data_import(self):
        """Rows created by Data Import (frappe.flags.in_import) are enqueued with announce_in_thread=False, decided in request context where the flag is set."""
        doc = _build_leave_doc()
        with _in_import_flag(True), patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            after_insert(doc, method=None)
        bg_call = next(c for c in mock_enqueue.call_args_list if c.args[0] is send_leave_notification_bg)
        self.assertFalse(bg_call.kwargs["announce_in_thread"])

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


class TestSendLeaveNotificationBg(IntegrationTestCase):
    def test_posts_chat_message_to_approver_dm_with_application_blocks(self):
        """send_leave_notification_bg calls chat_postMessage with the approver's Slack DM channel when the approver has a Slack ID."""
        doc = _build_leave_doc(from_date="2026-06-15")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            _leave_override_env(doc=doc, settings=settings) as mocks,
        ):
            send_leave_notification_bg(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], "U-approver")
        mocks.error_log.assert_not_called()

    def test_posts_thread_reply_to_attendance_channel_when_leave_covers_today(self):
        """When the leave covers today, send_attendance_updates=1, and today's summary ts is set, also posts a broadcast thread reply to the attendance channel."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            _leave_override_env(doc=doc) as mocks,
        ):
            send_leave_notification_bg(doc)
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_count, 2)
        attendance_call = next(
            c
            for c in mock_slack.slack_app.client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == SUMMARY_TS
        )
        self.assertEqual(attendance_call.kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertTrue(attendance_call.kwargs["reply_broadcast"])
        mocks.error_log.assert_not_called()

    def test_does_not_post_thread_reply_when_attendance_channel_empty(self):
        """With no attendance channel configured (SLACK_CHANNEL_ID None) the thread reply is skipped but the approver DM is still sent."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = None
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
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        approver_call = mock_slack.slack_app.client.chat_postMessage.call_args
        self.assertEqual(approver_call.kwargs["channel"], "U-approver")
        self.assertNotIn("thread_ts", approver_call.kwargs)

    def test_does_not_post_thread_reply_when_attendance_updates_disabled(self):
        """The attendance-channel thread reply does not fire when send_attendance_updates=0."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            _leave_override_env(doc=doc, settings=settings) as mocks,
        ):
            send_leave_notification_bg(doc)
        for call in mock_slack.slack_app.client.chat_postMessage.call_args_list:
            self.assertNotIn("thread_ts", call.kwargs)
        mocks.error_log.assert_not_called()

    def test_skips_thread_post_when_announce_in_thread_is_false(self):
        """With announce_in_thread=False (imported rows) the thread helper is never called; the approver DM still goes out."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.post_same_day_leave_to_attendance_thread") as mock_thread,
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            _leave_override_env(doc=doc),
        ):
            send_leave_notification_bg(doc, announce_in_thread=False)
        mock_thread.assert_not_called()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["channel"], "U-approver")

    def test_still_posts_approver_dm_when_thread_reply_fails(self):
        """A failure while posting the attendance-thread reply is logged and does not block the approver DM."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.post_same_day_leave_to_attendance_thread",
                side_effect=RuntimeError("slack down"),
            ),
            _leave_override_env(doc=doc) as mocks,
        ):
            send_leave_notification_bg(doc)
        mocks.error_log.assert_called_once()
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["channel"], "U-approver")

    def test_skips_approver_dm_when_approver_has_no_slack_id(self):
        """When the approver's Slack ID cannot be resolved, the approver DM is not posted (silently). Other side effects still run."""
        doc = _build_leave_doc(from_date="2026-06-15")
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID

        # The approver lookup raises; the applicant lookup returns a Slack id.
        def side_effect(*args, **kwargs):
            if kwargs.get("user_email") == "approver@x.com":
                raise RuntimeError("no meta")
            return "U-applicant"

        mock_slack.get_slack_user_id.side_effect = side_effect
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(doc=doc, settings=settings),
        ):
            send_leave_notification_bg(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_approver_dm_shows_requested_days_and_balance_before_request(self):
        """The approver DM shows the requested days and the balance before the request for a leave type with an allocation."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        text = _blocks_text(_get_approver_blocks(doc, balance_on=10.0))
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", text)
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", text)

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
        self.assertIn(":moneybag: *Balance before this request*\n_3 day(s)_", text)

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
        self.assertIn(":moneybag: *Balance before this request*\n_Not applicable (unpaid)_", text)
        self.assertNotIn("Insufficient balance", text)
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", text)

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
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Second Half_", text)
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
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Half Day_", text)

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
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Half day on Jun 16, 2026 (Tue) — First Half_", text)
        self.assertIn(":bar_chart: *Requested*\n_2.5 day(s)_", text)
        self.assertNotIn(":hourglass_flowing_sand: *Duration*\n_Half Day_", text)
        self.assertNotIn(":hourglass_flowing_sand: *Duration*\n_First Half_\n", text)

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
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Half day on Jun 16, 2026 (Tue)_\n", text)

    def test_approver_dm_computes_balance_even_when_doc_carries_zero(self):
        """leave_balance is a NOT NULL column, so a server-created leave reads back as 0.0 after any reload before the job runs (a Notification on save reloads the doc): the balance must still come from HRMS, not show 0 with a false warning."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-16", total_leave_days=2.0, leave_balance=0.0)
        mocks = _run_leave_notification_bg(doc, balance_on=12.0)
        mocks["get_leave_balance_on"].assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(":moneybag: *Balance before this request*\n_12 day(s)_", text)
        self.assertNotIn("Insufficient balance", text)

    def test_approver_dm_shows_a_real_zero_balance_with_the_warning(self):
        """When HRMS really reports 0, the DM shows 0 day(s) and the insufficient-balance warning."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-16", total_leave_days=2.0, leave_balance=0.0)
        mocks = _run_leave_notification_bg(doc, balance_on=0.0)
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(":moneybag: *Balance before this request*\n_0 day(s)_", text)
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
        self.assertIn(":moneybag: *Balance before this request*\n_7 day(s)_", text)
        self.assertNotIn("Insufficient balance", text)
        mocks["generate_error_log"].assert_not_called()

    def test_approver_dm_falls_back_to_the_stored_balance_when_the_lookup_fails(self):
        """When get_leave_balance_on raises but the form stored a balance, that value is shown (and the failure logged)."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=10.0)
        mocks = _run_leave_notification_bg(doc, balance_on=PermissionError("no leave access"))
        mocks["generate_error_log"].assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", text)

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
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", text)

    def test_approver_dm_always_computes_balance_except_for_lwp(self):
        """get_leave_balance_on is called even when the doc carries leave_balance (the stored value can be stale or a NOT NULL 0), and never for a Leave Without Pay type."""
        stored = _run_leave_notification_bg(_build_leave_doc(leave_balance=10.0), balance_on=9.0)
        stored["get_leave_balance_on"].assert_called_once()
        self.assertIn(
            ":moneybag: *Balance before this request*\n_9 day(s)_",
            _blocks_text(_approver_call(stored["slack"]).kwargs["blocks"]),
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
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", text)
        self.assertNotIn("Insufficient balance", text)

    def test_approver_dm_does_not_add_the_request_back_for_a_submitted_rejected_leave(self):
        """A rejected leave is submitted without a ledger entry, so nothing was deducted and nothing is added back."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0)
        doc.docstatus = 1
        doc.status = "Rejected"
        text = _blocks_text(_get_approver_blocks(doc, balance_on=7.0))
        self.assertIn(":moneybag: *Balance before this request*\n_7 day(s)_", text)

    def test_attendance_thread_keeps_today_period_for_multi_day_half_day_leave_starting_today(self):
        """For a multi-day leave starting today with the half day today, the attendance thread says _(First-Half)_ while the approver DM carries the full duration text."""
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
        self.assertIn("is on leave today. _(First-Half)_", thread_call.kwargs["blocks"][0]["text"]["text"])
        approver_text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(
            ":hourglass_flowing_sand: *Duration*\n_Half day on Jun 10, 2026 (Wed) — First Half_", approver_text
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
        self.assertIn("is on leave today. _(Full Day)_", thread_call.kwargs["blocks"][0]["text"]["text"])

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
        self.assertIn("is on leave today. _(Full Day)_", thread_call.kwargs["blocks"][0]["text"]["text"])
        approver_text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn(
            ":hourglass_flowing_sand: *Duration*\n_Half day on Jun 12, 2026 (Fri) — Second Half_", approver_text
        )

    def test_approver_dm_shows_full_day_for_leave_without_half_day(self):
        """A leave with half_day unset is labelled Full Day."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", half_day=0)
        text = _blocks_text(_get_approver_blocks(doc))
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Full Day_", text)


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
        self.assertEqual(
            blocks[2]["elements"][0]["text"],
            ":id: <https://erp.example.com/app/leave-application/HR-LAP-0050|HR-LAP-0050>"
            "  ·  :clock3: Submitted Jun 09, 2026 (Tue)",
        )

        field_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        field_texts = [f["text"] for section in field_sections for f in section["fields"]]
        self.assertIn(":label: *Leave Type*\n_Casual Leave_", field_texts)
        self.assertIn(":date: *From*\n_Jun 15, 2026 (Mon)_", field_texts)
        self.assertIn(":date: *To*\n_Jun 17, 2026 (Wed)_", field_texts)
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Full Day_", field_texts)
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", field_texts)
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", field_texts)

        text = _blocks_text(blocks)
        self.assertIn(":speech_balloon: *Reason*", text)
        self.assertEqual(_reason_quote(blocks), {"type": "text", "text": "vacation", "style": {"italic": True}})
        self.assertNotIn("Insufficient balance", text)
        self.assertNotIn("Half Day:", text)

        actions = next(b for b in blocks if b.get("type") == "actions")
        self.assertEqual(actions["block_id"], "leave_actions_block")
        self.assertEqual(
            [e["action_id"] for e in actions["elements"]], ["leave_approve", "leave_reject", "ignore_open_leave"]
        )
        self.assertTrue(all(e["value"] == "HR-LAP-0050" for e in actions["elements"][:2]))
        # The link button posts a block_action too; "ignore" action ids are dropped by the endpoint
        self.assertEqual(actions["elements"][2]["url"], "https://erp.example.com/app/leave-application/HR-LAP-0050")

    def test_omits_requested_and_balance_fields_when_not_provided(self):
        """When total_days and leave_balance are None, only the duration field is added and no warning appears."""
        blocks = format_leave_application_blocks(**self._default_kwargs(total_days=None, leave_balance=None))
        text = _blocks_text(blocks)
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Full Day_", text)
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
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", text)


class TestFormatLeaveSubmissionBlocks(IntegrationTestCase):
    def _default_kwargs(self, **overrides):
        kwargs = {
            "leave_id": "HR-LAP-0050",
            "leave_link": "https://erp.example.com/app/leave-application/HR-LAP-0050",
            "employee_name": "EMP-001",
            "leave_type": "Casual Leave",
            "leave_submission_date": "2026-06-09 09:00:00",
            "user_slack": "U-applicant",
            "from_date": "2026-06-15",
            "to_date": "2026-06-17",
            "reason": "vacation",
            "duration": "Full Day",
            "total_days": 3.0,
            "leave_balance": 10.0,
        }
        kwargs.update(overrides)
        return kwargs

    def test_shares_the_detail_fields_with_the_approver_message(self):
        """The applicant DM carries the same type/submitted, from/to and duration/requested/balance fields as the approver DM, then the reason."""
        blocks = format_leave_submission_blocks(**self._default_kwargs())
        approver = format_leave_application_blocks(
            leave_id="HR-LAP-0050",
            leave_link="https://erp.example.com/app/leave-application/HR-LAP-0050",
            employee_name="<@U-applicant>",
            leave_type="Casual Leave",
            leave_submission_date="Jun 09, 2026 (Tue)",
            from_date="Jun 15, 2026 (Mon)",
            to_date="Jun 17, 2026 (Wed)",
            duration="Full Day",
            reason="vacation",
            total_days=3.0,
            leave_balance=10.0,
        )
        fields = lambda bs: [b for b in bs if b.get("type") == "section" and "fields" in b]  # noqa: E731
        self.assertEqual(fields(blocks), fields(approver))
        field_texts = [f["text"] for section in fields(blocks) for f in section["fields"]]
        self.assertEqual(
            field_texts,
            [
                ":label: *Leave Type*\n_Casual Leave_",
                ":hourglass_flowing_sand: *Duration*\n_Full Day_",
                ":date: *From*\n_Jun 15, 2026 (Mon)_",
                ":date: *To*\n_Jun 17, 2026 (Wed)_",
                ":bar_chart: *Requested*\n_3 day(s)_",
                ":moneybag: *Balance before this request*\n_10 day(s)_",
            ],
        )
        self.assertEqual(blocks[0]["text"]["text"], ":memo: Leave Request Submitted")
        self.assertIn("Hello<@U-applicant>!", blocks[1]["text"]["text"])
        self.assertEqual(blocks[2], approver[2])
        self.assertEqual(
            blocks[2]["elements"][0]["text"],
            ":id: <https://erp.example.com/app/leave-application/HR-LAP-0050|HR-LAP-0050>"
            "  ·  :clock3: Submitted Jun 09, 2026 (Tue)",
        )
        self.assertEqual(blocks[-4]["text"]["text"], ":speech_balloon: *Reason*")
        self.assertEqual(_reason_quote(blocks), {"type": "text", "text": "vacation", "style": {"italic": True}})
        # The applicant message closes with a small status line; no buttons on this side
        self.assertEqual(blocks[-1]["elements"][0]["text"], "*Status:* :stopwatch: *Open*")
        self.assertNotIn("actions", [b["type"] for b in blocks])

    def test_omits_requested_and_balance_when_not_given_and_appends_the_decision(self):
        blocks = format_leave_submission_blocks(
            **self._default_kwargs(total_days=None, leave_balance=None, status="Rejected", status_by="<@U-approver>")
        )
        text = _blocks_text(blocks)
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Full Day_", text)
        self.assertNotIn("*Requested:*", text)
        self.assertNotIn("Balance before this request", text)
        self.assertEqual(blocks[0]["text"]["text"], ":x: Leave Request Rejected")
        self.assertEqual(blocks[-1]["elements"][0]["text"], "*Status:* :x: *Rejected* by <@U-approver>")


class TestPostSameDayLeaveToAttendanceThread(IntegrationTestCase):
    def test_posts_reply_and_stores_ts_when_leave_covers_today_after_summary(self):
        """A leave covering today, applied after the summary, posts one broadcast reply in the summary thread and stores its ts and the channel ID Slack reported on the Leave Application without touching modified."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(result, REPLY_TS)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)
        self.assertEqual(kwargs["thread_ts"], SUMMARY_TS)
        self.assertTrue(kwargs["reply_broadcast"])
        mocks.set_value.assert_called_once_with(
            "Leave Application",
            doc.name,
            {
                ATTENDANCE_REPLY_TS_FIELD: REPLY_TS,
                ATTENDANCE_REPLY_CHANNEL_FIELD: REPLY_CHANNEL,
                ATTENDANCE_REPLY_TEXT_FIELD: "<@U-applicant> is on leave today. _(Full Day)_",
            },
            update_modified=False,
        )
        mocks.error_log.assert_not_called()

    def test_posts_reply_in_the_channel_stored_by_the_morning_post(self):
        """When Slack Settings carries the channel ID the summary was posted in, the reply goes there (the thread lives there), not to the setting, which may have been changed since the morning."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock()
        settings.last_attendance_channel_id = "C0MORNING"
        with _leave_override_env(doc=doc, settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["channel"], "C0MORNING")

    def test_does_not_post_when_the_leave_was_withdrawn_before_the_job_ran(self):
        """The job carries the inserted row (status Open); when the committed row is Rejected, Cancelled, cancelled outright or gone by the time it runs, nothing is posted and nothing stored (the withdrawal hook found no reply to strike, so this must not create one)."""
        for state in (
            {"status": "Rejected", "docstatus": 0},
            {"status": "Cancelled", "docstatus": 2},
            {"status": "Approved", "docstatus": 2},
            None,
        ):
            with self.subTest(state=state):
                doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
                mock_slack = _build_slack_mock()
                with _leave_override_env(doc=doc, db_states=[state]) as mocks:
                    result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
                self.assertIsNone(result)
                mock_slack.slack_app.client.chat_postMessage.assert_not_called()
                mocks.set_value.assert_not_called()

    def test_does_not_post_when_the_dates_no_longer_cover_today(self):
        """The queued row covered today, but the draft was edited before the job ran: the committed row decides, nothing is posted."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(
            doc=doc, db_states=[{"status": "Open", "docstatus": 0, "from_date": "2026-06-11", "to_date": "2026-06-11"}]
        ):
            self.assertIsNone(post_same_day_leave_to_attendance_thread(doc, slack=mock_slack))
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_reply_text_comes_from_the_committed_row(self):
        """A half day set on the draft after it was queued shows in the reply, because the text is built from the committed row."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
            _leave_override_env(
                doc=doc, db_states=[{"status": "Open", "docstatus": 0, "half_day": 1, "half_day_date": "2026-06-10"}]
            ),
        ):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIn("_(Half Day)_", _thread_reply_text(mock_slack))

    def test_retries_the_withdrawal_once_when_the_row_is_gone_and_the_cleanup_fails(self):
        """A delete that commits while Slack is posting leaves no row to keep the reference; if striking the reply fails transiently, one withdrawal job carrying the new ts, channel and text is queued so the announcement is not orphaned."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        mock_slack.slack_app.client.chat_update.side_effect = RuntimeError("timeout")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(doc=doc, db_states=[{"status": "Open", "docstatus": 0}, None]) as mocks,
        ):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mocks.error_log.assert_called_once()
        mock_enqueue.assert_called_once()
        self.assertIs(mock_enqueue.call_args.args[0], withdraw_attendance_reply_bg)
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual((kwargs["reply_ts"], kwargs["channel"]), (REPLY_TS, REPLY_CHANNEL))
        self.assertEqual(kwargs["reply_text"], "<@U-applicant> is on leave today. _(Full Day)_")

    def test_does_not_post_twice_when_a_reply_is_already_stored(self):
        """A duplicate job (or a retry) finds the reply reference already stored and posts nothing."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(
            doc=doc, db_states=[{"status": "Open", "docstatus": 0, ATTENDANCE_REPLY_TS_FIELD: REPLY_TS}]
        ):
            self.assertIsNone(post_same_day_leave_to_attendance_thread(doc, slack=mock_slack))
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_strikes_the_reply_it_just_posted_when_withdrawn_meanwhile(self):
        """A withdrawal committed while Slack was posting found nothing stored and enqueued nothing, so the post job itself strikes the reply through and clears the reference."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(
            doc=doc, db_states=[{"status": "Open", "docstatus": 0}, {"status": "Cancelled", "docstatus": 2}]
        ) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(result, REPLY_TS)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        _assert_struck_through(mock_slack, REPLY_TS, channel=REPLY_CHANNEL)
        self.assertEqual(
            mocks.set_value.call_args.args[2],
            {ATTENDANCE_REPLY_TS_FIELD: None, ATTENDANCE_REPLY_CHANNEL_FIELD: None, ATTENDANCE_REPLY_TEXT_FIELD: None},
        )

    def test_reply_wording_mentions_employee_and_full_day(self):
        """The reply reads "<mention> is on leave today. _(Full Day)_" for a full-day leave when mention_user is on."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "<@U-applicant> is on leave today. _(Full Day)_")

    def test_reply_uses_employee_name_when_mentions_disabled(self):
        """When mention_user is off, the reply names the employee instead of mentioning their Slack user."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee_name="Alice")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with _leave_override_env(doc=doc, settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "Alice is on leave today. _(Full Day)_")

    def test_escapes_slack_control_characters_in_employee_name(self):
        """When mentions are off, &, < and > in the employee name are escaped so Slack renders the name verbatim."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee_name="A & B <C>")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with _leave_override_env(doc=doc, settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "A &amp; B &lt;C&gt; is on leave today. _(Full Day)_")

    def test_checks_employee_status_by_employee_id(self):
        """The Active check reads the status of the leave's employee from the Employee doctype."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee="EMP-042")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc) as mocks:
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mocks.get_value.assert_any_call("Employee", "EMP-042", "status")

    def test_posts_nothing_when_leave_is_not_open_or_approved(self):
        """A leave inserted as Rejected (or Cancelled) is not announced even though it covers today."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_posts_nothing_when_summary_not_yet_posted_today(self):
        """When last_attendance_date is an earlier day, nothing is posted and no ts is stored (the summary will include the leave)."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-09")
        with _leave_override_env(doc=doc, settings=settings) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_posts_nothing_when_summary_ts_is_missing(self):
        """When Slack Settings has no last_attendance_msg_ts there is no thread to reply in, so nothing is posted."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(last_attendance_msg_ts=None)
        with _leave_override_env(doc=doc, settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_posts_backdated_multi_day_leave_that_covers_today(self):
        """A leave that started before today and ends after today is announced, not only leave that starts today."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_posts_nothing_when_leave_does_not_cover_today(self):
        """A leave starting tomorrow is not announced in today's thread."""
        doc = _build_leave_doc(from_date="2026-06-11", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_works_when_from_and_to_date_are_date_objects(self):
        """from_date/to_date given as datetime.date (as when the doc is built from Python with getdate()) are detected as covering today."""
        doc = _build_leave_doc(from_date=date(2026, 6, 10), to_date=date(2026, 6, 11))
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_works_when_from_and_to_date_are_strings(self):
        """from_date/to_date given as YYYY-MM-DD strings (as sent by the Desk form) are detected as covering today."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-11")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_posts_nothing_when_today_is_a_holiday_for_the_employee(self):
        """An employee for whom get_employees_on_holiday marks today a holiday is not announced: the summary drops them by the same rule."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc, employee_on_holiday=True) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.on_holiday.assert_called_once_with([doc.employee], date(2026, 6, 10))

    def test_posts_reply_when_today_is_a_working_day_for_the_employee(self):
        """When the employee is not on holiday today the reply is posted; the check happens after the cheaper gates."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc) as mocks:
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        mocks.on_holiday.assert_called_once_with([doc.employee], date(2026, 6, 10))

    def test_posts_nothing_when_employee_is_not_active(self):
        """An employee whose status is not Active is not announced, matching the summary query."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc, employee_status="Left"):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_shows_full_day_when_half_day_is_on_another_date(self):
        """A multi-day leave with its half day on a different date shows Full Day for today."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12", half_day=1, half_day_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env(doc=doc):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "<@U-applicant> is on leave today. _(Full Day)_")

    def test_shows_half_day_when_half_day_is_today(self):
        """A leave whose half day falls on today shows Half Day (standalone installs without the rtCamp half field)."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12", half_day=1, half_day_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
            _leave_override_env(doc=doc),
        ):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "<@U-applicant> is on leave today. _(Half Day)_")


class TestOnUpdateWithdrawAttendanceReply(IntegrationTestCase):
    def test_rejection_same_day_deletes_reply_and_clears_ts(self):
        """Rejecting a leave on the day its stored reply was posted enqueues the withdrawal after commit with the leave name, the database ts and what the job needs to rebuild the text; the job edits the reply into struck-through text ending in (Cancelled) and clears the field."""
        reply_ts = _slack_ts_at_noon_utc(TODAY)
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline) as mock_enqueue,
            _leave_override_env(stored_ts=reply_ts) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        self.assertEqual(mock_enqueue.call_args.args[0], withdraw_attendance_reply_bg)
        self.assertEqual(mock_enqueue.call_args.kwargs["queue"], "short")
        self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(mock_enqueue.call_args.kwargs["leave_name"], doc.name)
        self.assertEqual(mock_enqueue.call_args.kwargs["reply_ts"], reply_ts)
        self.assertEqual(mock_enqueue.call_args.kwargs["channel"], REPLY_CHANNEL)
        self.assertEqual(mock_enqueue.call_args.kwargs["employee"], doc.employee)
        self.assertEqual(mock_enqueue.call_args.kwargs["employee_name"], doc.employee_name)
        self.assertEqual(mock_enqueue.call_args.kwargs["day_period"], "Full Day")
        self.assertNotIn("doc", mock_enqueue.call_args.kwargs)
        _assert_struck_through(mock_slack, reply_ts)
        mocks.set_value.assert_called_once_with(
            "Leave Application",
            doc.name,
            {ATTENDANCE_REPLY_TS_FIELD: None, ATTENDANCE_REPLY_CHANNEL_FIELD: None, ATTENDANCE_REPLY_TEXT_FIELD: None},
            update_modified=False,
        )
        mocks.error_log.assert_not_called()

    def test_force_cancel_that_keeps_the_status_still_strikes_the_reply(self):
        """A cancel with ignore_validate skips HRMS before_cancel, so the status stays Approved while docstatus becomes 2; the summary drops the leave on docstatus, so the reply is struck through too."""
        reply_ts = _slack_ts_at_noon_utc(TODAY)
        doc = _build_leave_doc(status="Approved", status_changed=False)
        doc.docstatus = 2
        doc.has_value_changed.side_effect = lambda fieldname: fieldname == "docstatus"
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=reply_ts) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_cancel")
        _assert_struck_through(mock_slack, reply_ts)
        mocks.error_log.assert_not_called()

    def test_submit_does_not_strike_the_reply(self):
        """Submitting (docstatus 0 -> 1) changes docstatus but is not a cancel: nothing is enqueued."""
        doc = _build_leave_doc(status="Approved", status_changed=False)
        doc.docstatus = 1
        doc.has_value_changed.side_effect = lambda fieldname: fieldname == "docstatus"
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)),
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_prefers_the_committed_row_over_the_values_passed_in(self):
        """The job reads the committed row under the lock: a withdrawal saved before the post job stored the reply enqueued nothing to pass along, but the row carries the reference by the time the job runs, and that is what gets struck through."""
        reply_ts = _slack_ts_at_noon_utc(TODAY)
        doc = _build_leave_doc(status="Cancelled", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline) as mock_enqueue,
            _leave_override_env(
                stored_ts=None,
                db_states=[
                    {
                        "status": "Cancelled",
                        "docstatus": 2,
                        ATTENDANCE_REPLY_TS_FIELD: reply_ts,
                        ATTENDANCE_REPLY_CHANNEL_FIELD: "C0LATER",
                    }
                ],
            ) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_cancel")
        self.assertIsNone(mock_enqueue.call_args.kwargs["reply_ts"])
        _assert_struck_through(mock_slack, reply_ts, channel="C0LATER")
        mocks.error_log.assert_not_called()

    def test_submitted_leave_saved_without_a_change_does_not_strike_the_reply(self):
        """A submitted, Approved leave (docstatus 1) whose status did not change enqueues nothing."""
        doc = _build_leave_doc(status="Approved", status_changed=False)
        doc.docstatus = 1
        doc.has_value_changed.side_effect = lambda fieldname: False
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)),
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update_after_submit")
        mock_enqueue.assert_not_called()

    def test_reads_ts_from_database_not_from_doc(self):
        """The removal uses the ts stored in the database, not the value carried on the doc (which a client could set): a doc claiming the summary ts with a real reply stored edits only the stored reply."""
        reply_ts = _slack_ts_at_noon_utc(TODAY)
        doc = _build_leave_doc(status="Rejected", status_changed=True, attendance_reply_ts=SUMMARY_TS)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=reply_ts) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mocks.get_value.assert_any_call(
            "Leave Application", doc.name, list(ATTENDANCE_REPLY_FIELDS), as_dict=True, ignore=True
        )
        _assert_struck_through(mock_slack, reply_ts)
        mocks.error_log.assert_not_called()

    def test_ignores_ts_on_doc_when_database_has_none(self):
        """A ts present only on the doc (never stored by the bot) is ignored: the job reads the row, finds nothing and edits nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True, attendance_reply_ts=_slack_ts_at_noon_utc(TODAY))
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline) as mock_enqueue,
            _leave_override_env(stored_ts=None) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        self.assertIsNone(mock_enqueue.call_args.kwargs["reply_ts"])
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.error_log.assert_not_called()

    def test_cancellation_same_day_deletes_reply(self):
        """Cancelling an approved leave on the day of its thread reply strikes it through with (Cancelled), wired through on_cancel."""
        doc = _build_leave_doc(status="Cancelled", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_cancel")
        _assert_struck_through(mock_slack, _slack_ts_at_noon_utc(TODAY))
        mocks.error_log.assert_not_called()

    def test_rejection_on_a_later_day_does_not_delete_old_reply(self):
        """Rejecting a leave the day after its thread reply was posted leaves the old reply alone: the job finds a ts from another day and edits nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc("2026-06-09")) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.error_log.assert_not_called()

    def test_does_nothing_when_status_unchanged(self):
        """An edit that does not change status (even on a rejected leave with a stored ts) enqueues nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=False)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)),
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_does_nothing_when_status_changes_to_approved(self):
        """Approval keeps the thread reply: a status change to Approved enqueues nothing."""
        doc = _build_leave_doc(status="Approved", status_changed=True)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)),
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_does_nothing_without_stored_ts(self):
        """A rejected leave that never had a thread reply (nothing stored when the job runs) edits nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=None) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.error_log.assert_not_called()

    def test_does_not_enqueue_when_the_leave_does_not_cover_today(self):
        """A withdrawn leave that does not cover today never had a same-day reply: nothing is enqueued."""
        doc = _build_leave_doc(status="Rejected", status_changed=True, from_date="2026-06-11", to_date="2026-06-12")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)),
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()

    def test_discard_same_day_deletes_reply(self):
        """Discarding a draft (HRMS db_sets status to Cancelled, so only on_discard fires) strikes through a reply posted today with (Cancelled)."""
        doc = _build_leave_doc(status="Cancelled", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_discard")
        _assert_struck_through(mock_slack, _slack_ts_at_noon_utc(TODAY))
        mocks.error_log.assert_not_called()

    def test_counts_reply_as_today_in_site_timezone(self):
        """A reply at 20:00 UTC on the previous day is 01:30 today in Asia/Kolkata, so a same-day rejection still removes it."""
        reply_ts = f"{datetime(2026, 6, 9, 20, 0, tzinfo=UTC).timestamp():.6f}"
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        with (
            patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(today=TODAY, stored_ts=reply_ts) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["reply_ts"], reply_ts)
        mocks.error_log.assert_not_called()

    def test_logs_and_does_not_raise_on_malformed_ts(self):
        """A stored ts that is not a number is logged by the job and never blocks the reject/cancel; nothing is edited."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts="not-a-ts") as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mocks.error_log.assert_called_once()
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_logs_and_does_not_raise_when_enqueue_fails(self):
        """A queue outage while scheduling the removal is logged and does not block the reject/cancel."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=ConnectionError("redis down")),
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc(TODAY)) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mocks.error_log.assert_called_once()


class TestOnTrashWithdrawAttendanceReply(IntegrationTestCase):
    def test_deleting_leave_same_day_deletes_reply(self):
        """Deleting a leave (a draft never runs on_update/on_cancel) strikes through a reply posted today with (Cancelled), regardless of status, using the stored ts and leave details read before the row goes."""
        reply_ts = _slack_ts_at_noon_utc(TODAY)
        doc = _build_leave_doc(status="Open")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline) as mock_enqueue,
            _leave_override_env(stored_ts=reply_ts, db_states=[None]) as mocks,
        ):
            on_trash_withdraw_attendance_reply(doc, method="on_trash")
        self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(mock_enqueue.call_args.kwargs["reply_ts"], reply_ts)
        _assert_struck_through(mock_slack, reply_ts)
        mocks.error_log.assert_not_called()

    def test_deleting_leave_on_a_later_day_keeps_old_reply(self):
        """Deleting a leave the day after its reply was posted leaves the old reply alone (the row is gone when the job runs, so it falls back to the values read before the delete)."""
        doc = _build_leave_doc(status="Open")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline),
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc("2026-06-09"), db_states=[None]) as mocks,
        ):
            on_trash_withdraw_attendance_reply(doc, method="on_trash")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.error_log.assert_not_called()


STORED_REF = frappe._dict({ATTENDANCE_REPLY_TS_FIELD: REPLY_TS, ATTENDANCE_REPLY_CHANNEL_FIELD: REPLY_CHANNEL})


class TestRestoreAttendanceReplyTs(IntegrationTestCase):
    def _build_real_doc(self, **fields):
        """Build a real (not inserted) Leave Application Document so doc.get/doc.set behave as in a hook."""
        return frappe.get_doc(
            {
                "doctype": "Leave Application",
                "name": "HR-LAP-TEST-158",
                "employee": "EMP-001",
                "leave_type": "Casual Leave",
                "from_date": "2026-06-10",
                "to_date": "2026-06-10",
                **fields,
            }
        )

    def test_restores_stored_ts_when_in_memory_value_is_empty(self):
        """A saved doc whose in-memory ts is empty gets the database value copied back so db_update does not erase it."""
        doc = self._build_real_doc()
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=STORED_REF) as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_called_once_with(
            "Leave Application", doc.name, list(ATTENDANCE_REPLY_FIELDS), as_dict=True, ignore=True, for_update=True
        )
        self.assertEqual(doc.get(ATTENDANCE_REPLY_TS_FIELD), REPLY_TS)
        self.assertEqual(doc.get(ATTENDANCE_REPLY_CHANNEL_FIELD), REPLY_CHANNEL)

    def test_replaces_client_supplied_ts_on_existing_doc_with_database_value(self):
        """A non-empty ts sent by the client for an existing doc is overwritten by the database value: only the bot's jobs own this field."""
        doc = self._build_real_doc(**{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS})
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=STORED_REF) as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_called_once_with(
            "Leave Application", doc.name, list(ATTENDANCE_REPLY_FIELDS), as_dict=True, ignore=True, for_update=True
        )
        self.assertEqual(doc.get(ATTENDANCE_REPLY_TS_FIELD), REPLY_TS)
        self.assertEqual(doc.get(ATTENDANCE_REPLY_CHANNEL_FIELD), REPLY_CHANNEL)

    def test_clears_client_supplied_ts_on_existing_doc_when_database_is_empty(self):
        """When nothing is stored, a client-supplied ts on an existing doc is cleared rather than kept."""
        doc = self._build_real_doc(
            **{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS, ATTENDANCE_REPLY_CHANNEL_FIELD: "C0FORGED"}
        )
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=None),
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_CHANNEL_FIELD))

    def test_clears_client_supplied_ts_on_new_doc(self):
        """A ts sent with a new doc is blanked without reading the database: an insert can never pre-load a message for deletion."""
        doc = self._build_real_doc(
            **{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS, ATTENDANCE_REPLY_CHANNEL_FIELD: "C0FORGED"}
        )
        with (
            patch.object(doc, "is_new", return_value=True),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value") as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_not_called()
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_CHANNEL_FIELD))

    def test_clears_ts_on_nameless_docs_without_reading_database(self):
        """A doc built in memory and never inserted reports is_new() False but has no name; its ts is blanked and the database is not queried without a name."""
        doc = self._build_real_doc(
            name=None, **{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS, ATTENDANCE_REPLY_CHANNEL_FIELD: "C0FORGED"}
        )
        self.assertFalse(doc.is_new())
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value") as mock_get_value:
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_not_called()
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_CHANNEL_FIELD))

    def test_reply_reference_fields_are_installed_from_fixtures(self):
        """The field names the code reads and writes exist on Leave Application: every other test mocks the database, so this is the one check that the constants match the fixture."""
        meta = frappe.get_meta("Leave Application")
        for fieldname in ATTENDANCE_REPLY_FIELDS:
            self.assertTrue(meta.has_field(fieldname), fieldname)


class TestLeaveApplicationHooks(IntegrationTestCase):
    def test_every_leave_application_doc_event_handler_resolves(self):
        """Every dotted path registered under doc_events["Leave Application"] imports to a callable."""
        events = frappe.get_hooks("doc_events").get("Leave Application", {})
        self.assertTrue(events)
        for event, paths in events.items():
            for path in paths:
                self.assertTrue(callable(frappe.get_attr(path)), f"{event}: {path}")

    def test_attendance_reply_handlers_are_wired_to_expected_events(self):
        """The restore and removal handlers are registered on the events that can erase or decide a leave."""
        events = frappe.get_hooks("doc_events").get("Leave Application", {})
        restore = f"{LEAVE_OVERRIDE_MODULE}.restore_attendance_reply_ts"
        remove = f"{LEAVE_OVERRIDE_MODULE}.on_update_withdraw_attendance_reply"
        trash = f"{LEAVE_OVERRIDE_MODULE}.on_trash_withdraw_attendance_reply"
        for event in ("before_validate", "before_update_after_submit", "before_cancel"):
            self.assertIn(restore, events.get(event, []), event)
        for event in ("on_update", "on_update_after_submit", "on_cancel", "on_discard"):
            self.assertIn(remove, events.get(event, []), event)
        self.assertIn(trash, events.get("on_trash", []))


class TestWithdrawAttendanceReplyBg(IntegrationTestCase):
    def test_clears_the_reference_only_for_a_terminal_channel_error_with_a_stored_channel(self):
        """channel_not_found is terminal when the stored channel ID was used, but not when the job fell back to the setting (which may be a name Slack rejects while the reply still exists)."""
        for channel, cleared in ((REPLY_CHANNEL, True), (None, False)):
            with self.subTest(channel=channel):
                mock_slack = _build_slack_mock()
                mock_slack.slack_app.client.chat_update.side_effect = SlackApiError(
                    "channel_not_found", {"ok": False, "error": "channel_not_found"}
                )
                with (
                    patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
                    _leave_override_env(stored_ts=TODAY_TS, stored_channel=channel) as mocks,
                ):
                    withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
                mocks.error_log.assert_called_once()
                self.assertEqual(mocks.set_value.called, cleared)

    def test_logs_and_clears_ts_when_the_reply_is_gone(self):
        """When Slack says the reply no longer exists (deleted by hand) the failure is logged, nothing raises, and the stored reference is cleared: there is nothing left to withdraw."""
        mock_slack = _build_slack_mock()
        mock_slack.slack_app.client.chat_update.side_effect = SlackApiError(
            "message_not_found", {"ok": False, "error": "message_not_found"}
        )
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(stored_ts=TODAY_TS) as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
        mocks.error_log.assert_called_once()
        mocks.set_value.assert_called_once_with(
            "Leave Application",
            "HR-LAP-0050",
            {ATTENDANCE_REPLY_TS_FIELD: None, ATTENDANCE_REPLY_CHANNEL_FIELD: None, ATTENDANCE_REPLY_TEXT_FIELD: None},
            update_modified=False,
        )

    def test_keeps_the_reference_on_a_transient_slack_failure(self):
        """A rate limit, timeout or outage is logged but the stored reference is kept, so a later withdrawal the same day still has a handle on the reply."""
        for failure in (SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"}), RuntimeError("timeout")):
            with self.subTest(failure=failure):
                mock_slack = _build_slack_mock()
                mock_slack.slack_app.client.chat_update.side_effect = failure
                with (
                    patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
                    _leave_override_env(stored_ts=TODAY_TS) as mocks,
                ):
                    withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
                mocks.error_log.assert_called_once()
                mocks.set_value.assert_not_called()

    def test_refuses_to_delete_the_summary_message(self):
        """A reply ts equal to the stored summary ts is never edited: the failure is logged and the field is left for inspection."""
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(last_attendance_msg_ts=TODAY_TS)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(settings=settings, stored_ts=TODAY_TS) as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.error_log.assert_called_once()
        mocks.set_value.assert_not_called()

    def test_strikes_through_the_text_as_posted_not_as_it_would_render_now(self):
        """The stored reply text is wrapped verbatim: a mention setting turned off (or a leave edited) since the post does not rewrite what the channel saw."""
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(
                settings=settings, stored_ts=TODAY_TS, stored_text="<@U-applicant> is on leave today. _(Full Day)_"
            ) as mocks,
        ):
            withdraw_attendance_reply_bg(
                leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **{**WITHDRAW_JOB_KWARGS, "day_period": "Second-Half"}
            )
        _assert_struck_through(mock_slack, TODAY_TS)  # mention and Full Day, as posted
        mocks.error_log.assert_not_called()

    def test_leaves_the_reply_alone_when_the_leave_is_back_in_the_summary(self):
        """A queued rejection job that runs after a later transition put the leave back to Open/Approved finds an announced row and edits nothing."""
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(stored_ts=TODAY_TS, db_states=[{"status": "Approved", "docstatus": 1}]) as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_does_nothing_without_ts(self):
        """The background job is a no-op when neither the row nor the call carries a ts."""
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(stored_ts=None) as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=None, **WITHDRAW_JOB_KWARGS)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_edits_in_the_stored_channel_not_the_configured_one(self):
        """The edit goes to the channel ID stored when the reply was posted, not to the attendance channel setting, which may hold a name that chat.update rejects."""
        mock_slack = _build_slack_mock()
        mock_slack.SLACK_CHANNEL_ID = "attendance"
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(stored_ts=TODAY_TS) as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **WITHDRAW_JOB_KWARGS)
        _assert_struck_through(mock_slack, TODAY_TS, channel=REPLY_CHANNEL)
        mocks.error_log.assert_not_called()

    def test_falls_back_to_the_configured_channel_without_a_stored_one(self):
        """A reply stored before the channel ID was (upgrade day) is edited in the configured attendance channel."""
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(stored_ts=TODAY_TS, stored_channel=None) as mocks,
        ):
            withdraw_attendance_reply_bg(
                leave_name="HR-LAP-0050", reply_ts=TODAY_TS, **{**WITHDRAW_JOB_KWARGS, "channel": None}
            )
        _assert_struck_through(mock_slack, TODAY_TS, channel=TEST_SLACK_CHANNEL_ID)
        mocks.error_log.assert_not_called()

    def test_uses_plain_name_when_mentions_are_off(self):
        """With mention_user off the struck-through text carries the escaped employee name, like the original reply."""
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(settings=settings, stored_ts=TODAY_TS),
        ):
            withdraw_attendance_reply_bg(
                leave_name="HR-LAP-0050",
                reply_ts=TODAY_TS,
                channel=REPLY_CHANNEL,
                employee="EMP-001",
                employee_name="Alice <A&B>",
                day_period="Second-Half",
            )
        _assert_struck_through(mock_slack, TODAY_TS, name="Alice &lt;A&amp;B&gt;", day_period="Second-Half")
        self.assertEqual(
            mock_slack.slack_app.client.chat_update.call_args.kwargs["text"],
            "Alice &lt;A&amp;B&gt; is on leave today (Cancelled)",
        )


class TestSendLeaveNotificationToApplicant(IntegrationTestCase):
    def test_posts_confirmation_dm_to_applicant_with_submission_blocks(self):
        """send_leave_notification_to_applicant posts a chat_postMessage to the applicant's Slack DM with the leave-submission blocks."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=False),
        ):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        # The submission blocks include a header that reads "Leave Request Submitted".
        header_text = kwargs["blocks"][0]["text"]["text"]
        self.assertIn("Leave Request Submitted", header_text)
        # The applicant sees the same details as the approver: duration, requested days and balance
        text = _blocks_text(kwargs["blocks"])
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Full Day_", text)
        self.assertIn(":bar_chart: *Requested*\n_3 day(s)_", text)
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", text)
        self.assertIn(":clock3: Submitted Jun 09, 2026 (Tue)", text)

    def test_applicant_dm_shows_the_half_day_period_and_no_balance_for_leave_without_pay(self):
        """A single-day second-half leave reads "Second Half"; a Leave Without Pay type has no allocation, so the balance reads "Not applicable"."""
        doc = _build_leave_doc(
            from_date="2026-06-10",
            to_date="2026-06-10",
            half_day=1,
            first_half_second_half="Second Half",
            total_leave_days=0.5,
        )
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = TEST_SLACK_USER_ID
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0) as mock_balance,
            patch(f"{LEAVE_OVERRIDE_MODULE}.custom_fields_exist", return_value=True),
        ):
            send_leave_notification_to_applicant(doc)
        text = _blocks_text(mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn(":hourglass_flowing_sand: *Duration*\n_Second Half_", text)
        self.assertIn(":bar_chart: *Requested*\n_0.5 day(s)_", text)
        self.assertIn(":moneybag: *Balance before this request*\n_Not applicable (unpaid)_", text)
        mock_balance.assert_not_called()

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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit") as mock_commit,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=None),
        ):
            send_leave_notification_to_applicant(doc)
        mock_store.assert_called_once_with(doc.name, channel="D0FSC0001", ts="1700000000.000001")
        mock_commit.assert_called_once()
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit"),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
        ):
            send_leave_notification_to_applicant(doc)
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["blocks"][0]["text"]["text"], ":white_check_mark: Leave Request Approved")
        self.assertIn(
            "*Status:* :white_check_mark: *Approved* by <@U-approver>", kwargs["blocks"][-1]["elements"][0]["text"]
        )
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit") as mock_commit,
        ):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_store.assert_not_called()
        mock_commit.assert_not_called()

    def test_posts_and_stores_under_the_per_leave_lock(self):
        """The reference check, the post, the store and its commit all happen while the per-leave lock is held, so the decision job cannot interleave or read an empty reference."""
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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit", side_effect=lambda: order.append("commit")),
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
        # The reference is committed before the lock is released, or a waiting worker still reads it empty
        self.assertEqual(order, ["lock", "ref", "post", "store", "commit", "unlock"])

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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
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

    def test_force_cancel_that_keeps_the_status_enqueues_cancelled(self):
        """A cancel with ignore_validate skips HRMS before_cancel, so the status stays Approved while docstatus becomes 2: the applicant must still be told the leave is Cancelled."""
        doc = _build_leave_doc(
            status="Approved", docstatus=2, status_changed=False, docstatus_changed=True, modified_by="hr@x.com"
        )
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_cancel")
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["status"], "Cancelled")

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

    def test_cancelled_without_a_recorded_user_is_not_attributed_to_the_approver(self):
        """A cancellation with no recorded user passes decided_by=None rather than naming the leave approver."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2, status_changed=True, modified_by=None)
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_cancel")
        self.assertIsNone(mock_enqueue.call_args.kwargs["decided_by"])

    def test_reports_administrator_when_administrator_decided(self):
        """A decision made as Administrator (a scheduled auto-approval, or someone logged in as Administrator) is attributed to Administrator, not to the approver who did not act."""
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True, modified_by="Administrator")
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            on_update_notify_applicant(doc, method="on_update")
        self.assertEqual(mock_enqueue.call_args.kwargs["decided_by"], "Administrator")

    def test_falls_back_to_leave_approver_when_no_user_was_recorded(self):
        doc = _build_leave_doc(status="Approved", docstatus=1, status_changed=True, modified_by=None)
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
        mock_ref.assert_called_once_with("HR-LAP-RESTORE", for_update=True)
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
            # Leave Type.is_lwp -> 0 (paid), anything else (the decider's full name) -> "Approver Person"
            stack.enter_context(
                patch(
                    f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value",
                    side_effect=lambda doctype, *a, **k: 0 if doctype == "Leave Type" else "Approver Person",
                )
            )
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0))
            mock_store = stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.store_applicant_message_ref"))
            stack.enter_context(patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.commit"))
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
        status_line = kwargs["blocks"][-1]["elements"][0]["text"]
        self.assertIn("*Status:* :white_check_mark: *Approved* by <@U-approver>", status_line)
        # The decision time is a Slack date token (reader's own clock), rendered in italics after the decider
        self.assertIn("by <@U-approver> · _<!date^", status_line)
        self.assertTrue(status_line.endswith("|Jun 10, 2026 at 10:00 AM>_"))
        # The rewritten DM carries the balance before the request (an approved, submitted leave adds its days back)
        self.assertIn(":moneybag: *Balance before this request*\n_13 day(s)_", _blocks_text(kwargs["blocks"]))

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
        status_line = kwargs["blocks"][-1]["elements"][0]["text"]
        self.assertIn("*Status:* :x: *Rejected* by Approver Person", status_line)

    def test_cancelled_without_decider_shows_bare_status(self):
        """A cancellation with no attributable user renders the status line as just 'Cancelled'."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2, status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, mention_user=1, current_status="Cancelled"):
            send_leave_decision_to_applicant(doc=doc, status="Cancelled", decided_by=None)
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertTrue(
            kwargs["blocks"][-1]["elements"][0]["text"].startswith("*Status:* :no_entry_sign: *Cancelled* · _<!date^")
        )

    def test_cancelled_uses_cancelled_header(self):
        """A cancellation rewrites the header to 'Leave Request Cancelled' and names the user who cancelled."""
        doc = _build_leave_doc(status="Cancelled", status_changed=True)
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with self._patched(mock_slack, mention_user=0, current_status="Cancelled"):
            send_leave_decision_to_applicant(doc=doc, status="Cancelled", decided_by="hr@x.com")
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["blocks"][0]["text"]["text"], ":no_entry_sign: Leave Request Cancelled")
        self.assertIn(
            "*Status:* :no_entry_sign: *Cancelled* by Approver Person", kwargs["blocks"][-1]["elements"][0]["text"]
        )

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
        self.assertIn(
            "*Status:* :white_check_mark: *Approved* by Approver Person", kwargs["blocks"][-1]["elements"][0]["text"]
        )

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
        status_line = mock_slack.slack_app.client.chat_update.call_args.kwargs["blocks"][-1]["elements"][0]["text"]
        self.assertIn("*Approved* by Tom &amp; Jerry &lt;HR&gt;", status_line)


class TestReasonBlocks(IntegrationTestCase):
    def test_reason_is_rendered_literally_in_an_italic_quote(self):
        """mrkdwn control characters and mentions in the applicant's reason stay text: a rich_text quote is not parsed."""
        blocks = reason_blocks("my_friend's wedding_\n\n*urgent* <!channel> & <https://evil|Approve here>")
        self.assertEqual(blocks[0]["text"]["text"], ":speech_balloon: *Reason*")
        self.assertEqual(blocks[1]["type"], "rich_text")
        self.assertEqual(blocks[1]["elements"][0]["type"], "rich_text_quote")
        self.assertEqual(
            _reason_quote(blocks),
            {
                "type": "text",
                "text": "my_friend's wedding_\n\n*urgent* <!channel> & <https://evil|Approve here>",
                "style": {"italic": True},
            },
        )

    def test_empty_reason_reads_no_reason_provided(self):
        self.assertEqual(_reason_quote(reason_blocks("  \n"))["text"], "No reason provided")

    def test_keeps_paragraph_breaks_and_caps_very_long_reasons(self):
        self.assertEqual(_reason_quote(reason_blocks("first  \n\nsecond\n"))["text"], "first\n\nsecond")
        text = _reason_quote(reason_blocks("x" * 3500))["text"]
        self.assertEqual(len(text), 3000)
        self.assertTrue(text.endswith("…"))


class TestFormatDecider(IntegrationTestCase):
    def test_administrator_is_named_without_a_slack_lookup(self):
        """A scheduled auto-approval runs as Administrator, who has no Slack identity: no lookup, no Error Log, just the name."""
        mock_slack = MagicMock()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value="Administrator"):
            self.assertEqual(_format_decider(mock_slack, "Administrator"), "Administrator")
        mock_slack.get_slack_user_id.assert_not_called()


class TestDiscardAndTrashNotifyApplicant(IntegrationTestCase):
    def test_discard_queues_a_cancelled_decision_for_the_applicant(self):
        """Discarding a draft fires only on_discard; the applicant's DM must stop reading Open."""
        doc = _build_leave_doc(status="Cancelled", docstatus=2)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.session") as mock_session,
        ):
            mock_session.user = "alice@x.com"
            on_discard_notify_applicant(doc, method="on_discard")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIs(mock_enqueue.call_args.args[0], send_leave_decision_to_applicant)
        self.assertEqual(kwargs["status"], "Cancelled")
        self.assertEqual(kwargs["decided_by"], "alice@x.com")
        self.assertTrue(kwargs["enqueue_after_commit"])

    def test_trash_reads_the_dm_reference_first_and_queues_the_rewrite(self):
        """The reference and the balance must be read while the row exists; the job gets everything explicitly."""
        doc = _build_leave_doc()
        with (
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=("D0FSC0001", "1700000000.000001")
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.session") as mock_session,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
        ):
            mock_session.user = "hr@x.com"
            on_trash_notify_applicant(doc, method="on_trash")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIs(mock_enqueue.call_args.args[0], send_leave_deleted_to_applicant)
        self.assertEqual((kwargs["channel"], kwargs["ts"]), ("D0FSC0001", "1700000000.000001"))
        # An open leave is cancelled by whoever deletes it, now
        self.assertEqual((kwargs["status"], kwargs["decided_by"]), ("Cancelled", "hr@x.com"))
        self.assertIsNotNone(kwargs["decided_at"])
        self.assertEqual((kwargs["leave_balance"], kwargs["unpaid"]), (10.0, False))

    def test_trash_of_a_decided_leave_rewrites_with_the_decision_taken(self):
        """A rejected draft's DM should already read Rejected, but the decision job may still be queued or have failed and
        cannot run once the row is gone: the rewrite carries the decision, its decider and time, never the deleter."""
        doc = _build_leave_doc(
            status="Rejected", docstatus=0, modified_by="approver@x.com", modified="2026-06-11 11:00:00"
        )
        with (
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=("D0FSC0001", "1700000000.000001")
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.session") as mock_session,
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
        ):
            mock_session.user = "hr@x.com"
            on_trash_notify_applicant(doc, method="on_trash")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(
            (kwargs["status"], kwargs["decided_by"], kwargs["decided_at"]),
            ("Rejected", "approver@x.com", "2026-06-11 11:00:00"),
        )
        # A cancelled document reads Cancelled whatever its status column says, attributed to its last writer
        doc = _build_leave_doc(status="Approved", docstatus=2, modified_by="alice@x.com")
        with (
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=("D0FSC0001", "1700000000.000001")
            ),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on", return_value=10.0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
        ):
            on_trash_notify_applicant(doc, method="on_trash")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual((kwargs["status"], kwargs["decided_by"]), ("Cancelled", "alice@x.com"))

    def test_trash_without_a_stored_dm_queues_nothing(self):
        doc = _build_leave_doc()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_applicant_message_ref", return_value=(None, None)),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
        ):
            on_trash_notify_applicant(doc, method="on_trash")
        mock_enqueue.assert_not_called()

    def test_deleted_leave_dm_is_rewritten_as_cancelled_without_a_balance_lookup(self):
        """The row is gone: the DM is edited into a cancellation naming the deleter at the deletion time, and HRMS is not asked for a balance."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        slack_ids = {"hr@x.com": "U-hr", "approver@x.com": "U-approver"}
        mock_slack.get_slack_user_id.side_effect = lambda *a, **k: (
            "U-applicant" if "employee_id" in k else slack_ids.get(k.get("user_email"))
        )
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.get_leave_balance_on") as mock_balance,
        ):
            send_leave_deleted_to_applicant(
                doc,
                channel="D0FSC0001",
                ts="1700000000.000001",
                decided_by="hr@x.com",
                decided_at=datetime(2026, 6, 12, 9, 30),
                leave_balance=10.0,
            )
        # The hook looked the balance up while the row existed; the job must not ask HRMS again
        mock_balance.assert_not_called()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual((kwargs["channel"], kwargs["ts"]), ("D0FSC0001", "1700000000.000001"))
        self.assertEqual(kwargs["text"], "Your leave request has been cancelled")
        status_line = kwargs["blocks"][-1]["elements"][0]["text"]
        self.assertIn("*Status:* :no_entry_sign: *Cancelled* by <@U-hr> · _<!date^", status_line)
        self.assertTrue(status_line.endswith("|Jun 12, 2026 at 9:30 AM>_"))
        self.assertIn(":moneybag: *Balance before this request*\n_10 day(s)_", _blocks_text(kwargs["blocks"]))

    def test_deleted_leave_dm_logs_when_the_applicant_has_no_slack_id(self):
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.return_value = None
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_log,
        ):
            send_leave_deleted_to_applicant(doc, channel="D0FSC0001", ts="1700000000.000001")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        self.assertEqual(mock_log.call_args.kwargs["title"], "Applicant Slack ID not found")

    def test_deleted_leave_dm_refuses_a_reference_that_is_not_a_dm(self):
        """Same guard as the decision job: never edit anything but the applicant's DM."""
        doc = _build_leave_doc()
        mock_slack = MagicMock()
        mock_slack.get_slack_user_id.side_effect = _build_slack_user_lookup()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=0),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_single_value", return_value=1),
            patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_log,
        ):
            send_leave_deleted_to_applicant(doc, channel="C0MORNING", ts="1700000000.000001", decided_by="hr@x.com")
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mock_log.assert_called_once()

    def test_hooks_register_the_discard_and_trash_notifications(self):
        leave_events = frappe.get_hooks("doc_events")["Leave Application"]
        self.assertIn(f"{LEAVE_OVERRIDE_MODULE}.on_discard_notify_applicant", leave_events["on_discard"])
        self.assertIn(f"{LEAVE_OVERRIDE_MODULE}.on_trash_notify_applicant", leave_events["on_trash"])

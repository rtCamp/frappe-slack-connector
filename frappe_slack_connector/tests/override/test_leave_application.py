import contextlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.override.leave_application import (
    ATTENDANCE_REPLY_CHANNEL_FIELD,
    ATTENDANCE_REPLY_FIELDS,
    ATTENDANCE_REPLY_TEXT_FIELD,
    ATTENDANCE_REPLY_TS_FIELD,
    after_insert,
    format_leave_application_blocks,
    on_trash_withdraw_attendance_reply,
    on_update_withdraw_attendance_reply,
    post_same_day_leave_to_attendance_thread,
    restore_attendance_reply_ts,
    send_leave_notification_bg,
    send_leave_notification_to_applicant,
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
    status_changed=False,
    attendance_reply_ts=None,
):
    """Build a MagicMock that mimics a Leave Application doc with the fields the override code reads.

    ``attendance_reply_ts`` is the value carried on the doc itself; the removal code must ignore it
    and read the database instead (see ``_leave_override_env(stored_ts=...)``).
    """
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
    doc.custom_first_halfsecond_half = first_half_second_half
    doc.total_leave_days = total_leave_days
    doc.leave_balance = leave_balance
    doc.creation = creation
    doc.status = status
    doc.custom_slack_attendance_reply_ts = attendance_reply_ts
    doc.has_value_changed.return_value = status_changed
    # No before-save copy unless a test sets one: the handlers then read the stored reference from the database
    doc.get_doc_before_save.return_value = None
    doc.get.side_effect = lambda fieldname, default=None: getattr(doc, fieldname, default)
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


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg (announcing in the thread) and send_leave_notification_to_applicant, both on the short queue and only after the insert commits (the thread job writes its ts back to the row)."""
        doc = _build_leave_doc()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
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

    def test_does_not_post_thread_reply_when_attendance_updates_disabled(self):
        """The attendance-channel thread reply does not fire when send_attendance_updates=0."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(send_attendance_updates=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(doc=doc, settings=settings),
        ):
            send_leave_notification_bg(doc)
        for call in mock_slack.slack_app.client.chat_postMessage.call_args_list:
            self.assertNotIn("thread_ts", call.kwargs)

    def test_skips_thread_post_when_announce_in_thread_is_false(self):
        """With announce_in_thread=False (imported rows) the thread helper is never called; the approver DM still goes out."""
        doc = _build_leave_doc(from_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.post_same_day_leave_to_attendance_thread") as mock_thread,
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
        with patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack):
            send_leave_notification_to_applicant(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)
        # The submission blocks include a header that reads "Leave Request Submitted".
        header_text = kwargs["blocks"][0]["text"]["text"]
        self.assertIn("Leave Request Submitted", header_text)

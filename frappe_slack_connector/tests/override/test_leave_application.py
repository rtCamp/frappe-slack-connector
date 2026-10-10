import contextlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.override.leave_application import (
    ATTENDANCE_REPLY_TS_FIELD,
    after_insert,
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


def _slack_ts_at_noon_utc(day: str) -> str:
    """Return a Slack-style ``ts`` for 12:00 UTC on ``day`` (YYYY-MM-DD).

    Noon UTC lands on the same calendar day in every site timezone between
    UTC-11 and UTC+11, so the date derived from the ts is stable in tests.
    """
    moment = datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)
    return f"{moment.timestamp():.6f}"


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
    doc.creation = creation
    doc.status = status
    doc.custom_slack_attendance_reply_ts = attendance_reply_ts
    doc.has_value_changed.return_value = status_changed
    doc.get.side_effect = lambda fieldname, default=None: getattr(doc, fieldname, default)
    return doc


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
    settings.mention_user = mention_user
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
    mock_slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "ts": REPLY_TS}
    mock_slack.slack_app.client.chat_update.return_value = {"ok": True}
    return mock_slack


@contextlib.contextmanager
def _leave_override_env(
    *, settings=None, employee_status="Active", employee_on_holiday=False, today=TODAY, stored_ts=None
):
    """Patch the Frappe surface the leave override reads: Slack Settings, today's date, the Employee status lookup, the stored reply ts and the ts write-back.

    ``frappe.db.get_value`` / ``get_single_value`` are patched with scoped side effects that answer only the
    lookups the override makes and delegate every other call to the real function, so Frappe internals
    (controller lookup, system settings) keep working. ``generate_error_log`` is patched too, so an
    unexpected exception shows up as an assertion instead of a real Error Log insert.

    Yields a namespace with the ``get_value``, ``set_value`` and ``error_log`` mocks.
    """
    settings = settings if settings is not None else _build_slack_settings_mock()
    real_get_value = frappe.db.get_value
    real_get_single_value = frappe.db.get_single_value

    # Same parameter names as frappe.db.get_value so delegated keyword calls bind correctly.
    def fake_get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
        if doctype == "Employee" and fieldname == "status":
            return employee_status
        if doctype == "Leave Application" and fieldname == ATTENDANCE_REPLY_TS_FIELD:
            return stored_ts
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
        patch(f"{LEAVE_OVERRIDE_MODULE}.generate_error_log") as mock_error_log,
        patch(
            f"{LEAVE_OVERRIDE_MODULE}.get_employees_on_holiday",
            side_effect=lambda employees, on_date: set(employees) if employee_on_holiday else set(),
        ) as mock_on_holiday,
    ):
        yield SimpleNamespace(
            get_value=mock_get_value,
            set_value=mock_set_value,
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


WITHDRAW_JOB_KWARGS = {"employee": "EMP-001", "employee_name": "Alice", "day_period": "Full Day"}


def _assert_struck_through(mock_slack, reply_ts, *, name="<@U-applicant>", day_period="Full Day"):
    """Assert the reply was edited in place (not deleted) into the struck-through text ending in (Cancelled)."""
    label = "Cancelled"
    mock_slack.slack_app.client.chat_delete.assert_not_called()
    mock_slack.slack_app.client.chat_update.assert_called_once()
    kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
    assert kwargs["channel"] == TEST_SLACK_CHANNEL_ID, kwargs
    assert kwargs["ts"] == reply_ts, kwargs
    assert kwargs["blocks"][0]["text"]["text"] == f"~{name} is on leave today. _({day_period})_~ ({label})", kwargs
    assert f"({label})" in kwargs["text"], kwargs


def _run_enqueued_inline(method, **kwargs):
    """frappe.enqueue stand-in that runs the job synchronously with the job kwargs it was given."""
    job_kwargs = {k: v for k, v in kwargs.items() if k not in ("queue", "enqueue_after_commit")}
    return method(**job_kwargs)


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg (announcing in the thread) and send_leave_notification_to_applicant, both on the short queue."""
        doc = _build_leave_doc()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        calls = {call.args[0]: call.kwargs for call in mock_enqueue.call_args_list}
        self.assertIn(send_leave_notification_bg, calls)
        self.assertIn(send_leave_notification_to_applicant, calls)
        for kwargs in calls.values():
            self.assertEqual(kwargs["queue"], "short")
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
            _leave_override_env(settings=settings) as mocks,
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
            _leave_override_env() as mocks,
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
            _leave_override_env(settings=settings),
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
            _leave_override_env(),
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
            patch(
                f"{LEAVE_OVERRIDE_MODULE}.post_same_day_leave_to_attendance_thread",
                side_effect=RuntimeError("slack down"),
            ),
            _leave_override_env() as mocks,
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
            _leave_override_env(settings=settings),
        ):
            send_leave_notification_bg(doc)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()


class TestPostSameDayLeaveToAttendanceThread(IntegrationTestCase):
    def test_posts_reply_and_stores_ts_when_leave_covers_today_after_summary(self):
        """A leave covering today, applied after the summary, posts one broadcast reply in the summary thread and stores its ts on the Leave Application without touching modified."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env() as mocks:
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
            ATTENDANCE_REPLY_TS_FIELD,
            REPLY_TS,
            update_modified=False,
        )
        mocks.error_log.assert_not_called()

    def test_reply_wording_mentions_employee_and_full_day(self):
        """The reply reads "<mention> is on leave today. _(Full Day)_" for a full-day leave when mention_user is on."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "<@U-applicant> is on leave today. _(Full Day)_")

    def test_reply_uses_employee_name_when_mentions_disabled(self):
        """When mention_user is off, the reply names the employee instead of mentioning their Slack user."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee_name="Alice")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with _leave_override_env(settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "Alice is on leave today. _(Full Day)_")

    def test_escapes_slack_control_characters_in_employee_name(self):
        """When mentions are off, &, < and > in the employee name are escaped so Slack renders the name verbatim."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee_name="A & B <C>")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with _leave_override_env(settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "A &amp; B &lt;C&gt; is on leave today. _(Full Day)_")

    def test_checks_employee_status_by_employee_id(self):
        """The Active check reads the status of the leave's employee from the Employee doctype."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", employee="EMP-042")
        mock_slack = _build_slack_mock()
        with _leave_override_env() as mocks:
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mocks.get_value.assert_any_call("Employee", "EMP-042", "status")

    def test_posts_nothing_when_leave_is_not_open_or_approved(self):
        """A leave inserted as Rejected (or Cancelled) is not announced even though it covers today."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10", status="Rejected")
        mock_slack = _build_slack_mock()
        with _leave_override_env() as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_posts_nothing_when_summary_not_yet_posted_today(self):
        """When last_attendance_date is an earlier day, nothing is posted and no ts is stored (the summary will include the leave)."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(last_attendance_date="2026-06-09")
        with _leave_override_env(settings=settings) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_posts_nothing_when_summary_ts_is_missing(self):
        """When Slack Settings has no last_attendance_msg_ts there is no thread to reply in, so nothing is posted."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(last_attendance_msg_ts=None)
        with _leave_override_env(settings=settings):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_posts_backdated_multi_day_leave_that_covers_today(self):
        """A leave that started before today and ends after today is announced, not only leave that starts today."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_posts_nothing_when_leave_does_not_cover_today(self):
        """A leave starting tomorrow is not announced in today's thread."""
        doc = _build_leave_doc(from_date="2026-06-11", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_works_when_from_and_to_date_are_date_objects(self):
        """from_date/to_date given as datetime.date (as when the doc is built from Python with getdate()) are detected as covering today."""
        doc = _build_leave_doc(from_date=date(2026, 6, 10), to_date=date(2026, 6, 11))
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_works_when_from_and_to_date_are_strings(self):
        """from_date/to_date given as YYYY-MM-DD strings (as sent by the Desk form) are detected as covering today."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-11")
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()

    def test_posts_nothing_when_today_is_a_holiday_for_the_employee(self):
        """An employee for whom get_employees_on_holiday marks today a holiday is not announced: the summary drops them by the same rule."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env(employee_on_holiday=True) as mocks:
            result = post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertIsNone(result)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mocks.set_value.assert_not_called()
        mocks.on_holiday.assert_called_once_with([doc.employee], date(2026, 6, 10))

    def test_posts_reply_when_today_is_a_working_day_for_the_employee(self):
        """When the employee is not on holiday today the reply is posted; the check happens after the cheaper gates."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env() as mocks:
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        mocks.on_holiday.assert_called_once_with([doc.employee], date(2026, 6, 10))

    def test_posts_nothing_when_employee_is_not_active(self):
        """An employee whose status is not Active is not announced, matching the summary query."""
        doc = _build_leave_doc(from_date="2026-06-10", to_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with _leave_override_env(employee_status="Left"):
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_shows_full_day_when_half_day_is_on_another_date(self):
        """A multi-day leave with its half day on a different date shows Full Day for today."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12", half_day=1, half_day_date="2026-06-12")
        mock_slack = _build_slack_mock()
        with _leave_override_env():
            post_same_day_leave_to_attendance_thread(doc, slack=mock_slack)
        self.assertEqual(_thread_reply_text(mock_slack), "<@U-applicant> is on leave today. _(Full Day)_")

    def test_shows_half_day_when_half_day_is_today(self):
        """A leave whose half day falls on today shows Half Day (standalone installs without the rtCamp half field)."""
        doc = _build_leave_doc(from_date="2026-06-08", to_date="2026-06-12", half_day=1, half_day_date="2026-06-10")
        mock_slack = _build_slack_mock()
        with (
            patch(f"{ATTENDANCE_MODULE}.custom_fields_exist", return_value=False),
            _leave_override_env(),
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
        self.assertEqual(mock_enqueue.call_args.kwargs["employee"], doc.employee)
        self.assertEqual(mock_enqueue.call_args.kwargs["employee_name"], doc.employee_name)
        self.assertEqual(mock_enqueue.call_args.kwargs["day_period"], "Full Day")
        self.assertNotIn("doc", mock_enqueue.call_args.kwargs)
        _assert_struck_through(mock_slack, reply_ts)
        mocks.set_value.assert_called_once_with(
            "Leave Application",
            doc.name,
            ATTENDANCE_REPLY_TS_FIELD,
            None,
            update_modified=False,
        )
        mocks.error_log.assert_not_called()

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
        mocks.get_value.assert_any_call("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD)
        _assert_struck_through(mock_slack, reply_ts)
        mocks.error_log.assert_not_called()

    def test_ignores_ts_on_doc_when_database_has_none(self):
        """A ts present only on the doc (never stored by the bot) enqueues nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True, attendance_reply_ts=_slack_ts_at_noon_utc(TODAY))
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=None) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()
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
        """Rejecting a leave the day after its thread reply was posted leaves the old reply alone and enqueues nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue", side_effect=_run_enqueued_inline) as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc("2026-06-09")) as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mock_enqueue.assert_not_called()
        mock_slack.slack_app.client.chat_update.assert_not_called()
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
        """A rejected leave that never had a thread reply (no stored ts) enqueues nothing."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=None),
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
        """A stored ts that is not a number is logged and never blocks the reject/cancel; nothing is enqueued."""
        doc = _build_leave_doc(status="Rejected", status_changed=True)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts="not-a-ts") as mocks,
        ):
            on_update_withdraw_attendance_reply(doc, method="on_update")
        mocks.error_log.assert_called_once()
        mock_enqueue.assert_not_called()

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
            _leave_override_env(stored_ts=reply_ts) as mocks,
        ):
            on_trash_withdraw_attendance_reply(doc, method="on_trash")
        self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(mock_enqueue.call_args.kwargs["reply_ts"], reply_ts)
        _assert_struck_through(mock_slack, reply_ts)
        mocks.error_log.assert_not_called()

    def test_deleting_leave_on_a_later_day_keeps_old_reply(self):
        """Deleting a leave the day after its reply was posted leaves the old reply alone."""
        doc = _build_leave_doc(status="Open")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue,
            _leave_override_env(stored_ts=_slack_ts_at_noon_utc("2026-06-09")) as mocks,
        ):
            on_trash_withdraw_attendance_reply(doc, method="on_trash")
        mock_enqueue.assert_not_called()
        mocks.error_log.assert_not_called()


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
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=REPLY_TS) as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_called_once_with("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD)
        self.assertEqual(doc.get(ATTENDANCE_REPLY_TS_FIELD), REPLY_TS)

    def test_replaces_client_supplied_ts_on_existing_doc_with_database_value(self):
        """A non-empty ts sent by the client for an existing doc is overwritten by the database value: only the bot's jobs own this field."""
        doc = self._build_real_doc(**{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS})
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=REPLY_TS) as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_called_once_with("Leave Application", doc.name, ATTENDANCE_REPLY_TS_FIELD)
        self.assertEqual(doc.get(ATTENDANCE_REPLY_TS_FIELD), REPLY_TS)

    def test_clears_client_supplied_ts_on_existing_doc_when_database_is_empty(self):
        """When nothing is stored, a client-supplied ts on an existing doc is cleared rather than kept."""
        doc = self._build_real_doc(**{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS})
        with (
            patch.object(doc, "is_new", return_value=False),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value", return_value=None),
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))

    def test_clears_client_supplied_ts_on_new_doc(self):
        """A ts sent with a new doc is blanked without reading the database: an insert can never pre-load a message for deletion."""
        doc = self._build_real_doc(**{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS})
        with (
            patch.object(doc, "is_new", return_value=True),
            patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value") as mock_get_value,
        ):
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_not_called()
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))

    def test_clears_ts_on_nameless_docs_without_reading_database(self):
        """A doc built in memory and never inserted reports is_new() False but has no name; its ts is blanked and the database is not queried without a name."""
        doc = self._build_real_doc(name=None, **{ATTENDANCE_REPLY_TS_FIELD: SUMMARY_TS})
        self.assertFalse(doc.is_new())
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.db.get_value") as mock_get_value:
            restore_attendance_reply_ts(doc, method="before_validate")
        mock_get_value.assert_not_called()
        self.assertIsNone(doc.get(ATTENDANCE_REPLY_TS_FIELD))


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
    def test_logs_and_clears_ts_when_delete_fails(self):
        """A chat_update failure (for example the reply was already deleted by hand) is logged, does not raise, and still clears the stored ts."""
        mock_slack = _build_slack_mock()
        mock_slack.slack_app.client.chat_update.side_effect = RuntimeError("message_not_found")
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env() as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=REPLY_TS, **WITHDRAW_JOB_KWARGS)
        mocks.error_log.assert_called_once()
        mocks.set_value.assert_called_once_with(
            "Leave Application",
            "HR-LAP-0050",
            ATTENDANCE_REPLY_TS_FIELD,
            None,
            update_modified=False,
        )

    def test_refuses_to_delete_the_summary_message(self):
        """A reply ts equal to the stored summary ts is never edited: the failure is logged and the field is left for inspection."""
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env() as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=SUMMARY_TS, **WITHDRAW_JOB_KWARGS)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.error_log.assert_called_once()
        mocks.set_value.assert_not_called()

    def test_does_nothing_without_ts(self):
        """The background job is a no-op when called without a ts."""
        mock_slack = _build_slack_mock()
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env() as mocks,
        ):
            withdraw_attendance_reply_bg(leave_name="HR-LAP-0050", reply_ts=None, **WITHDRAW_JOB_KWARGS)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        mocks.set_value.assert_not_called()

    def test_uses_plain_name_when_mentions_are_off(self):
        """With mention_user off the struck-through text carries the escaped employee name, like the original reply."""
        mock_slack = _build_slack_mock()
        settings = _build_slack_settings_mock(mention_user=0)
        with (
            patch(f"{LEAVE_OVERRIDE_MODULE}.SlackIntegration", return_value=mock_slack),
            _leave_override_env(settings=settings),
        ):
            withdraw_attendance_reply_bg(
                leave_name="HR-LAP-0050",
                reply_ts=REPLY_TS,
                employee="EMP-001",
                employee_name="Alice <A&B>",
                day_period="Second-Half",
            )
        _assert_struck_through(mock_slack, REPLY_TS, name="Alice &lt;A&amp;B&gt;", day_period="Second-Half")


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

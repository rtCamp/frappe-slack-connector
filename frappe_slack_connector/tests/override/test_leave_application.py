from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.override.leave_application import (
    after_insert,
    format_leave_application_blocks,
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
    first_half_second_half=None,
    total_leave_days=3.0,
    leave_balance=10.0,
    creation="2026-06-09 09:00:00",
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


def _run_leave_notification_bg(doc, *, custom_fields=False, is_lwp=0, attendance_updates=0, balance_on=None):
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


def _get_approver_blocks(doc, *, custom_fields=False, is_lwp=0, balance_on=None):
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


class TestAfterInsert(IntegrationTestCase):
    def test_enqueues_both_notification_jobs_on_short_queue(self):
        """after_insert enqueues send_leave_notification_bg and send_leave_notification_to_applicant, both on the short queue."""
        doc = _build_leave_doc()
        with patch(f"{LEAVE_OVERRIDE_MODULE}.frappe.enqueue") as mock_enqueue:
            after_insert(doc, method=None)
        self.assertEqual(mock_enqueue.call_count, 2)
        targets = [call.args[0] for call in mock_enqueue.call_args_list]
        self.assertIn(send_leave_notification_bg, targets)
        self.assertIn(send_leave_notification_to_applicant, targets)
        for call in mock_enqueue.call_args_list:
            self.assertEqual(call.kwargs["queue"], "short")
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

    def test_approver_dm_shows_requested_days_and_balance_before_request(self):
        """The approver DM shows the requested days and the balance before the request for a leave type with an allocation."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=10.0)
        text = _blocks_text(_get_approver_blocks(doc))
        self.assertIn("*Requested:*\n3 day(s)", text)
        self.assertIn("*Balance before this request:*\n10 day(s)", text)

    def test_approver_dm_warns_when_requested_days_exceed_balance(self):
        """When total_leave_days exceeds leave_balance the approver DM carries the insufficient-balance warning with the shortfall and resulting balance."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=1.0)
        text = _blocks_text(_get_approver_blocks(doc))
        self.assertIn(
            ":warning: *Insufficient balance:* this request exceeds the available balance by 2 day(s). "
            "Approving it will take the balance to -2.",
            text,
        )

    def test_approver_dm_has_no_warning_when_balance_is_sufficient(self):
        """No insufficient-balance warning is added when leave_balance covers total_leave_days (including when they are equal)."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=3.0)
        text = _blocks_text(_get_approver_blocks(doc))
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

    def test_approver_dm_shows_zero_balance_and_warning_for_non_lwp_leave(self):
        """A non-LWP leave with leave_balance=0.0 shows the stored balance (0 day(s)) and the warning; zero is not treated as missing and does not trigger the HRMS lookup."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-16", total_leave_days=2.0, leave_balance=0.0)
        mocks = _run_leave_notification_bg(doc, balance_on=99.0)
        mocks["get_leave_balance_on"].assert_not_called()
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
        )
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertIn("*Balance before this request:*\n7 day(s)", text)
        self.assertNotIn("Insufficient balance", text)
        mocks["generate_error_log"].assert_not_called()

    def test_approver_dm_omits_balance_and_logs_when_balance_lookup_fails(self):
        """When get_leave_balance_on raises, the error is logged, the balance block and warning are omitted, and the approver DM is still posted."""
        doc = _build_leave_doc(from_date="2026-06-15", to_date="2026-06-17", total_leave_days=3.0, leave_balance=None)
        mocks = _run_leave_notification_bg(doc, balance_on=PermissionError("no leave access"))
        mocks["generate_error_log"].assert_called_once()
        self.assertEqual(mocks["generate_error_log"].call_args.kwargs["title"], "Error fetching leave balance")
        mocks["slack"].slack_app.client.chat_postMessage.assert_called_once()
        text = _blocks_text(_approver_call(mocks["slack"]).kwargs["blocks"])
        self.assertNotIn("Balance before this request", text)
        self.assertNotIn("Insufficient balance", text)
        self.assertIn("*Requested:*\n3 day(s)", text)

    def test_approver_dm_does_not_compute_balance_for_lwp_or_when_stored(self):
        """get_leave_balance_on is not called when the doc already carries leave_balance, nor for a Leave Without Pay type."""
        stored = _run_leave_notification_bg(_build_leave_doc(leave_balance=10.0))
        stored["get_leave_balance_on"].assert_not_called()
        lwp = _run_leave_notification_bg(_build_leave_doc(leave_type="Leave Without Pay", leave_balance=None), is_lwp=1)
        lwp["get_leave_balance_on"].assert_not_called()

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

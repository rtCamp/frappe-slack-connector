from contextlib import ExitStack
from datetime import date as date_cls
from datetime import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.tasks.send_daily_reminder import (
    get_daily_norm_from_employee,
    send_reminder,
    send_slack_notification,
)
from frappe_slack_connector.tests import TEST_SLACK_USER_ID

REMINDER_MODULE = "frappe_slack_connector.tasks.send_daily_reminder"


def _build_settings_mock(
    *,
    timesheet_previousday_reminder=1,
    last_timesheet_notification_date=None,
    timesheet_daily_notification_time="10:00:00",
    reminder_template="Daily Reminder",
    allowed_departments=None,
):
    settings = MagicMock()
    settings.timesheet_previousday_reminder = timesheet_previousday_reminder
    settings.last_timesheet_notification_date = last_timesheet_notification_date
    settings.timesheet_daily_notification_time = timesheet_daily_notification_time
    settings.reminder_template = reminder_template
    settings.allowed_departments = allowed_departments or []
    return settings


def _make_get_all_router(
    *,
    employees=None,
    user_metas=None,
    half_day_leaves=None,
    timesheets=None,
    holidays=None,
    full_day_leaves=None,
):
    """Return a callable suitable for frappe.get_all.side_effect that routes by doctype + filters."""
    employees = employees or []
    user_metas = user_metas or []
    half_day_leaves = half_day_leaves or []
    timesheets = timesheets or []
    holidays = holidays or []
    full_day_leaves = full_day_leaves or []

    def router(doctype, *args, **kwargs):
        filters = kwargs.get("filters", {})
        if doctype == "Employee":
            return employees
        if doctype == "User Meta":
            return user_metas
        if doctype == "Leave Application":
            if filters.get("half_day") == 1:
                return half_day_leaves
            return full_day_leaves
        if doctype == "Timesheet":
            return timesheets
        if doctype == "Holiday":
            return holidays
        return []

    return router


def _enter_notification_patches(stack, *, router, holiday_list_for=None):
    """Apply the standard patch stack for send_slack_notification tests and return the slack mock."""
    mock_slack = MagicMock()
    mock_template = MagicMock()
    mock_template.response_html = "Hi {{ name }}!"
    stack.enter_context(patch(f"{REMINDER_MODULE}.SlackIntegration", return_value=mock_slack))
    # Patch getdate so the source's call doesn't reach Frappe's system-timezone chain
    # (which would otherwise hit our frappe.get_doc mock via client_cache and pickle-fail).
    stack.enter_context(patch(f"{REMINDER_MODULE}.getdate", return_value=date_cls(2026, 6, 15)))
    stack.enter_context(patch(f"{REMINDER_MODULE}.frappe.get_doc", return_value=mock_template))
    stack.enter_context(patch(f"{REMINDER_MODULE}.is_next_pms_installed", return_value=False))
    stack.enter_context(patch(f"{REMINDER_MODULE}.frappe.db.get_single_value", return_value=8))
    stack.enter_context(patch(f"{REMINDER_MODULE}.frappe.get_all", side_effect=router))
    stack.enter_context(
        patch(
            f"{REMINDER_MODULE}.get_holiday_list_for_employee",
            side_effect=lambda emp: (holiday_list_for or {}).get(emp),
        )
    )
    stack.enter_context(patch(f"{REMINDER_MODULE}.frappe.render_template", return_value="rendered"))
    stack.enter_context(patch(f"{REMINDER_MODULE}.time.sleep"))
    stack.enter_context(patch(f"{REMINDER_MODULE}.generate_error_log"))
    return mock_slack


class TestSendReminder(IntegrationTestCase):
    def test_returns_silently_when_reminder_disabled(self):
        """send_reminder returns silently when timesheet_previousday_reminder=0."""
        settings = _build_settings_mock(timesheet_previousday_reminder=0)
        with (
            patch(f"{REMINDER_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{REMINDER_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{REMINDER_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(11, 0))),
            ),
            patch(f"{REMINDER_MODULE}.send_slack_notification") as mock_send,
        ):
            send_reminder()
        mock_send.assert_not_called()
        settings.save.assert_not_called()

    def test_returns_silently_when_already_sent_today(self):
        """send_reminder returns silently when last_timesheet_notification_date equals today."""
        settings = _build_settings_mock(last_timesheet_notification_date="2026-06-15")
        with (
            patch(f"{REMINDER_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{REMINDER_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{REMINDER_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(11, 0))),
            ),
            patch(f"{REMINDER_MODULE}.get_time", return_value=time(10, 0)),
            patch(f"{REMINDER_MODULE}.send_slack_notification") as mock_send,
        ):
            send_reminder()
        mock_send.assert_not_called()

    def test_returns_silently_when_current_time_before_notification_time(self):
        """send_reminder returns silently when the current time is before timesheet_daily_notification_time."""
        settings = _build_settings_mock(timesheet_daily_notification_time="10:00:00")
        with (
            patch(f"{REMINDER_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{REMINDER_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{REMINDER_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(9, 0))),
            ),
            patch(f"{REMINDER_MODULE}.get_time", return_value=time(10, 0)),
            patch(f"{REMINDER_MODULE}.send_slack_notification") as mock_send,
        ):
            send_reminder()
        mock_send.assert_not_called()

    def test_persists_state_after_successful_run(self):
        """When guards pass, send_reminder calls send_slack_notification and writes today's date back onto Slack Settings."""
        settings = _build_settings_mock()
        with (
            patch(f"{REMINDER_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{REMINDER_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{REMINDER_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(11, 0))),
            ),
            patch(f"{REMINDER_MODULE}.get_time", return_value=time(10, 0)),
            patch(f"{REMINDER_MODULE}.send_slack_notification") as mock_send,
        ):
            send_reminder()
        mock_send.assert_called_once()
        self.assertEqual(settings.last_timesheet_notification_date, "2026-06-15")
        settings.save.assert_called_once_with(ignore_permissions=True)


class TestSendSlackNotification(IntegrationTestCase):
    def test_posts_rendered_template_to_eligible_employee(self):
        """An employee with a Slack ID, no leave/holiday, and under-logged hours receives a chat_postMessage."""
        employee = frappe._dict(name="EMP-001", employee_name="Alice", user_id="alice@x.com")
        router = _make_get_all_router(
            employees=[employee],
            user_metas=[frappe._dict(user="alice@x.com", custom_slack_userid=TEST_SLACK_USER_ID)],
            timesheets=[frappe._dict(employee="EMP-001", total_hours=2.0)],
        )
        with ExitStack() as stack:
            mock_slack = _enter_notification_patches(stack, router=router)
            send_slack_notification(
                reminder_template="Daily Reminder",
                allowed_departments=[frappe._dict(department="Engineering")],
            )
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_USER_ID)

    def test_skips_employee_meeting_daily_norm(self):
        """An employee whose summed Timesheet hours match or exceed the daily norm is not messaged."""
        employee = frappe._dict(name="EMP-002", employee_name="Bob", user_id="bob@x.com")
        router = _make_get_all_router(
            employees=[employee],
            user_metas=[frappe._dict(user="bob@x.com", custom_slack_userid="U-BOB")],
            timesheets=[frappe._dict(employee="EMP-002", total_hours=8.0)],
        )
        with ExitStack() as stack:
            mock_slack = _enter_notification_patches(stack, router=router)
            send_slack_notification(
                reminder_template="Daily Reminder",
                allowed_departments=[frappe._dict(department="Engineering")],
            )
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_skips_employee_on_full_day_leave(self):
        """An employee with a Leave Application covering the date (half_day=0) is not messaged."""
        employee = frappe._dict(name="EMP-003", employee_name="Carol", user_id="carol@x.com")
        router = _make_get_all_router(
            employees=[employee],
            user_metas=[frappe._dict(user="carol@x.com", custom_slack_userid="U-CAROL")],
            full_day_leaves=[frappe._dict(employee="EMP-003")],
        )
        with ExitStack() as stack:
            mock_slack = _enter_notification_patches(stack, router=router)
            send_slack_notification(
                reminder_template="Daily Reminder",
                allowed_departments=[frappe._dict(department="Engineering")],
            )
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_skips_employee_when_holiday_list_marks_date_as_holiday(self):
        """An employee whose holiday list contains the date is not messaged."""
        employee = frappe._dict(name="EMP-004", employee_name="Dan", user_id="dan@x.com")
        router = _make_get_all_router(
            employees=[employee],
            user_metas=[frappe._dict(user="dan@x.com", custom_slack_userid="U-DAN")],
            holidays=[frappe._dict(parent="Engineering Holiday List")],
        )
        with ExitStack() as stack:
            mock_slack = _enter_notification_patches(
                stack,
                router=router,
                holiday_list_for={"EMP-004": "Engineering Holiday List"},
            )
            send_slack_notification(
                reminder_template="Daily Reminder",
                allowed_departments=[frappe._dict(department="Engineering")],
            )
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_skips_employee_with_two_half_day_leaves(self):
        """An employee with two half-day leaves on the date counts as a full-day leave and is not messaged."""
        employee = frappe._dict(name="EMP-005", employee_name="Eve", user_id="eve@x.com")
        router = _make_get_all_router(
            employees=[employee],
            user_metas=[frappe._dict(user="eve@x.com", custom_slack_userid="U-EVE")],
            half_day_leaves=[
                frappe._dict(employee="EMP-005"),
                frappe._dict(employee="EMP-005"),
            ],
            timesheets=[frappe._dict(employee="EMP-005", total_hours=0)],
        )
        with ExitStack() as stack:
            mock_slack = _enter_notification_patches(stack, router=router)
            send_slack_notification(
                reminder_template="Daily Reminder",
                allowed_departments=[frappe._dict(department="Engineering")],
            )
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()


class TestGetDailyNormFromEmployee(IntegrationTestCase):
    def test_uses_standard_working_hours_when_no_custom_fields(self):
        """When custom_working_hours is None, get_daily_norm_from_employee falls back to standard_working_hours."""
        employee = frappe._dict(name="EMP-001")
        with patch(f"{REMINDER_MODULE}.is_next_pms_installed", return_value=False):
            result = get_daily_norm_from_employee(employee, standard_working_hours=8)
        self.assertEqual(result, 8)

    def test_divides_by_5_when_frequency_is_per_week(self):
        """When working_frequency != 'Per Day' (and next_pms installed), the daily norm is working_hour divided by 5."""
        employee = frappe._dict(custom_working_hours=40, custom_work_schedule="Per Week")
        with patch(f"{REMINDER_MODULE}.is_next_pms_installed", return_value=True):
            result = get_daily_norm_from_employee(employee, standard_working_hours=8)
        self.assertEqual(result, 8)

    def test_returns_working_hour_for_per_day_frequency(self):
        """When working_frequency='Per Day' and next_pms installed, the daily norm equals custom_working_hours."""
        employee = frappe._dict(custom_working_hours=6, custom_work_schedule="Per Day")
        with patch(f"{REMINDER_MODULE}.is_next_pms_installed", return_value=True):
            result = get_daily_norm_from_employee(employee, standard_working_hours=8)
        self.assertEqual(result, 6)

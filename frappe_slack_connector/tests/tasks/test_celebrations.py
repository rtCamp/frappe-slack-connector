import json
from datetime import date as date_cls
from datetime import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.frappe_slack_connector.doctype.slack_settings.slack_settings import SlackSettings
from frappe_slack_connector.tasks.celebrations import (
    MAX_CATCH_UP_DAYS,
    MAX_NON_WORKING_RUN_DAYS,
    OPT_OUT_FIELD,
    SLACK_SECTION_TEXT_LIMIT,
    build_anniversary_blocks,
    build_birthday_blocks,
    celebrations_channel,
    claim_celebrations_day,
    describe_event_day,
    get_active_employees,
    get_celebration_window,
    get_default_holiday_list,
    get_employees_with_anniversary,
    get_employees_with_birthday,
    send_celebrations,
    split_text,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, TEST_SLACK_USER_ID, TEST_SLACK_USER_ID_2

CELEBRATIONS_MODULE = "frappe_slack_connector.tasks.celebrations"
SLACK_SETTINGS_MODULE = "frappe_slack_connector.frappe_slack_connector.doctype.slack_settings.slack_settings"

# 2026-06-15 is a Monday; 2026-06-13 is the Saturday before it.
MONDAY = date_cls(2026, 6, 15)
SATURDAY = date_cls(2026, 6, 13)


def _build_settings_mock(
    *,
    send_birthday_updates=1,
    send_anniversary_updates=1,
    celebrations_channel_id="C0CELEB001",
    celebrations_time="09:00:00",
    birthday_message_template=None,
    anniversary_message_template=None,
    last_celebrations_date=None,
    mention_user=1,
):
    settings = MagicMock()
    settings.send_birthday_updates = send_birthday_updates
    settings.send_anniversary_updates = send_anniversary_updates
    settings.celebrations_channel_id = celebrations_channel_id
    settings.celebrations_time = celebrations_time
    settings.birthday_message_template = birthday_message_template
    settings.anniversary_message_template = anniversary_message_template
    settings.last_celebrations_date = last_celebrations_date
    settings.mention_user = mention_user
    return settings


def _build_employee(
    *,
    name="EMP-0001",
    employee_name="Alice Example",
    user_id="alice@example.com",
    company="Acme Inc",
    date_of_birth="1990-06-15",
    date_of_joining="2020-01-01",
    skip=0,
):
    return frappe._dict(
        {
            "name": name,
            "employee_name": employee_name,
            "user_id": user_id,
            "company": company,
            "date_of_birth": frappe.utils.getdate(date_of_birth) if date_of_birth else None,
            "date_of_joining": frappe.utils.getdate(date_of_joining) if date_of_joining else None,
            "custom_skip_celebration_announcements": skip,
        }
    )


def _build_slack_mock(post_side_effect=None):
    slack = MagicMock()
    slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID
    slack.slack_app.client.chat_postMessage.return_value = {"ok": True, "ts": "1700000000.000001"}
    if post_side_effect is not None:
        slack.slack_app.client.chat_postMessage.side_effect = post_side_effect
    return slack


def _patch_cache_claim(claimed=True, set_mock=None):
    """Patch only frappe.cache.set / make_key (never the whole cache object, which Frappe's meta loading uses).

    Returns (set_patch, make_key_patch) context managers; `set` reports `claimed` for the SET NX call
    unless an explicit `set_mock` is supplied.
    """
    return (
        patch(f"{CELEBRATIONS_MODULE}.frappe.cache.set", set_mock or MagicMock(return_value=claimed)),
        patch(f"{CELEBRATIONS_MODULE}.frappe.cache.make_key", side_effect=lambda key: f"test|{key}"),
    )


def _build_employee_meta(has_opt_out_field=True):
    """A stand-in for frappe.get_meta("Employee") so tests never touch site meta."""
    meta = MagicMock()
    meta.has_field.return_value = has_opt_out_field
    return meta


def _section_text(blocks: list) -> str:
    """Return the mrkdwn text of the single section block in a celebrations message."""
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 1
    return sections[0]["text"]["text"]


class TestCelebrationsChannel(IntegrationTestCase):
    def _run(self, settings, *, nowdate="2026-06-15", now_time=time(10, 0), holiday=False):
        """Run celebrations_channel with every collaborator patched; returns a MagicMock parent whose
        .set_single_value / .enqueue / .get_default_holiday_list children record calls in order."""
        calls = MagicMock()
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=nowdate),
            patch(
                f"{CELEBRATIONS_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=now_time)),
            ),
            patch(
                f"{CELEBRATIONS_MODULE}.get_default_holiday_list",
                side_effect=lambda: calls.get_default_holiday_list() and "Acme Holidays",
            ),
            patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=holiday),
            patch(f"{CELEBRATIONS_MODULE}.get_time", return_value=time(9, 0)),
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.set_single_value", calls.set_single_value),
            patch(f"{CELEBRATIONS_MODULE}.frappe.enqueue", calls.enqueue),
        ):
            celebrations_channel()
        return calls

    def test_returns_silently_when_both_event_types_disabled(self):
        """celebrations_channel does nothing when neither birthday nor anniversary updates are enabled."""
        settings = _build_settings_mock(send_birthday_updates=0, send_anniversary_updates=0)
        calls = self._run(settings)
        calls.enqueue.assert_not_called()
        calls.set_single_value.assert_not_called()

    def test_returns_silently_when_today_is_weekend(self):
        """celebrations_channel does nothing on Saturday/Sunday; weekend events are announced on the next working day."""
        settings = _build_settings_mock()
        calls = self._run(settings, nowdate="2026-06-13")
        calls.enqueue.assert_not_called()
        calls.set_single_value.assert_not_called()

    def test_returns_silently_when_today_is_holiday(self):
        """celebrations_channel does nothing when today is a holiday in the default holiday list."""
        settings = _build_settings_mock()
        calls = self._run(settings, holiday=True)
        calls.enqueue.assert_not_called()
        calls.set_single_value.assert_not_called()

    def test_returns_silently_before_celebrations_time(self):
        """celebrations_channel does nothing when the current time is before Slack Settings.celebrations_time."""
        settings = _build_settings_mock()
        calls = self._run(settings, now_time=time(8, 30))
        calls.enqueue.assert_not_called()
        calls.set_single_value.assert_not_called()

    def test_returns_silently_when_already_run_today(self):
        """celebrations_channel does nothing when last_celebrations_date is already today (idempotency)."""
        settings = _build_settings_mock(last_celebrations_date="2026-06-15")
        calls = self._run(settings)
        calls.enqueue.assert_not_called()
        calls.set_single_value.assert_not_called()

    def test_cheap_guards_run_before_holiday_lookup(self):
        """When already run today, celebrations_channel returns before looking up the holiday list."""
        settings = _build_settings_mock(last_celebrations_date="2026-06-15")
        calls = self._run(settings)
        calls.get_default_holiday_list.assert_not_called()

    def test_stamps_date_then_enqueues_deduplicated_job_when_guards_pass(self):
        """When all guards pass, celebrations_channel stamps last_celebrations_date via set_single_value
        (without touching modified) and only then enqueues a deduplicated send_celebrations job."""
        settings = _build_settings_mock(last_celebrations_date=None)
        calls = self._run(settings)
        calls.set_single_value.assert_called_once_with(
            "Slack Settings", "last_celebrations_date", "2026-06-15", update_modified=False
        )
        calls.enqueue.assert_called_once_with(
            send_celebrations,
            queue="short",
            enqueue_after_commit=True,
            job_id="celebrations::2026-06-15",
            deduplicate=True,
            date="2026-06-15",
            previous_run=None,
        )
        self.assertEqual([c[0] for c in calls.mock_calls[-2:]], ["set_single_value", "enqueue"])
        settings.save.assert_not_called()

    def test_passes_previous_run_date_to_the_job(self):
        """The last_celebrations_date read before stamping is handed to the job as previous_run for catch-up."""
        settings = _build_settings_mock(last_celebrations_date=date_cls(2026, 6, 10))
        calls = self._run(settings)
        self.assertEqual(calls.enqueue.call_args.kwargs["previous_run"], "2026-06-10")

    def test_no_events_today_posts_nothing_but_date_is_still_stamped(self):
        """With no birthdays or anniversaries today, nothing is posted to Slack but last_celebrations_date is still set."""
        settings = _build_settings_mock()
        slack = _build_slack_mock()

        def run_inline(fn, **kwargs):
            for key in ("queue", "enqueue_after_commit", "job_id", "deduplicate"):
                kwargs.pop(key, None)
            return fn(**kwargs)

        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value="2026-06-15"),
            patch(
                f"{CELEBRATIONS_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=None),
            patch(f"{CELEBRATIONS_MODULE}.get_time", return_value=time(9, 0)),
            patch(f"{CELEBRATIONS_MODULE}.frappe.enqueue", side_effect=run_inline),
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.set_single_value") as mock_stamp,
            patch(f"{CELEBRATIONS_MODULE}.claim_celebrations_day", return_value=True),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=slack),
            patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=[]),
        ):
            celebrations_channel()
        slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_stamp.assert_called_once_with(
            "Slack Settings", "last_celebrations_date", "2026-06-15", update_modified=False
        )


class TestGetActiveEmployees(IntegrationTestCase):
    def _query(self, has_opt_out_field):
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_meta", return_value=_build_employee_meta(has_opt_out_field)),
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_all", return_value=[]) as mock_get_all,
        ):
            result = get_active_employees()
        self.assertEqual(result, [])
        mock_get_all.assert_called_once()
        return mock_get_all.call_args

    def test_queries_only_active_employees(self):
        """get_active_employees asks the DB for Employee rows with status Active only, so inactive employees are excluded."""
        args, kwargs = self._query(has_opt_out_field=True)
        self.assertEqual(args[0], "Employee")
        self.assertEqual(kwargs["filters"], {"status": "Active"})
        for field in ("name", "employee_name", "user_id", "company", "date_of_birth", "date_of_joining"):
            self.assertIn(field, kwargs["fields"])
        self.assertIn(OPT_OUT_FIELD, kwargs["fields"])

    def test_leaves_opt_out_field_out_of_query_when_not_installed(self):
        """When the Employee custom field is not installed yet, it is not requested so the query cannot fail."""
        _args, kwargs = self._query(has_opt_out_field=False)
        self.assertNotIn(OPT_OUT_FIELD, kwargs["fields"])


class TestGetCelebrationWindow(IntegrationTestCase):
    def test_window_is_today_only_on_a_midweek_working_day(self):
        """On a Tuesday after a working Monday, the window covers only today."""
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(date_cls(2026, 6, 16), None)
        self.assertEqual((start, end), (date_cls(2026, 6, 16), date_cls(2026, 6, 16)))

    def test_window_on_monday_covers_the_weekend(self):
        """On a Monday, the window starts on the preceding Saturday."""
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(MONDAY, None)
        self.assertEqual((start, end), (SATURDAY, MONDAY))

    def test_window_extends_over_a_holiday_before_the_weekend(self):
        """A holiday on the Friday before the weekend is also folded into Monday's window."""
        friday = date_cls(2026, 6, 12)

        def holiday(holiday_list, day):
            return day == friday

        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", side_effect=holiday):
            start, end = get_celebration_window(MONDAY, "Acme Holidays")
        self.assertEqual((start, end), (friday, MONDAY))

    def test_window_catches_up_missed_working_days_since_previous_run(self):
        """With previous_run three working days ago, the window starts the day after previous_run."""
        thursday = date_cls(2026, 6, 18)
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(thursday, None, previous_run=MONDAY)
        self.assertEqual((start, end), (date_cls(2026, 6, 16), thursday))

    def test_window_uses_the_wider_of_catch_up_and_non_working_run(self):
        """When previous_run is yesterday (normal case) the weekend roll-back still applies on Monday."""
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(MONDAY, None, previous_run=date_cls(2026, 6, 12))
        self.assertEqual((start, end), (SATURDAY, MONDAY))

    def test_window_catch_up_is_capped_at_max_catch_up_days(self):
        """A previous_run far in the past (feature re-enabled after months) only extends the window back 7 days."""
        self.assertEqual(MAX_CATCH_UP_DAYS, 7)
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(MONDAY, None, previous_run=date_cls(2026, 1, 1))
        self.assertEqual((start, end), (date_cls(2026, 6, 8), MONDAY))

    def test_window_covers_a_non_working_run_longer_than_the_catch_up_cap(self):
        """A 12-day shutdown (holidays Wed 3 Jun to Fri 12 Jun plus the weekend) is rolled back in full on Monday 15 Jun."""
        self.assertGreater(MAX_NON_WORKING_RUN_DAYS, MAX_CATCH_UP_DAYS)
        shutdown_start = date_cls(2026, 6, 3)

        def holiday(holiday_list, day):
            return shutdown_start <= day <= date_cls(2026, 6, 12)

        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", side_effect=holiday):
            start, end = get_celebration_window(MONDAY, "Acme Holidays", previous_run=date_cls(2026, 6, 2))
        self.assertEqual((start, end), (shutdown_start, MONDAY))
        employee = _build_employee(date_of_birth="1990-06-03")
        result = get_employees_with_birthday(end, start_date=start, employees=[employee])
        self.assertEqual([r.event_date for r in result], [shutdown_start])

    def test_window_crosses_the_year_boundary(self):
        """A Monday 1 Jan run has a window starting on Saturday 30 Dec of the previous year."""
        with patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False):
            start, end = get_celebration_window(date_cls(2029, 1, 1), None)
        self.assertEqual((start, end), (date_cls(2028, 12, 30), date_cls(2029, 1, 1)))


class TestGetDefaultHolidayList(IntegrationTestCase):
    def test_returns_default_company_holiday_list(self):
        """get_default_holiday_list reads Global Defaults.default_company and returns that company's default_holiday_list."""
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.get_single_value", return_value="Acme Inc") as mock_single,
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.get_value", return_value="Acme Holidays") as mock_value,
        ):
            result = get_default_holiday_list()
        self.assertEqual(result, "Acme Holidays")
        mock_single.assert_called_once_with("Global Defaults", "default_company")
        mock_value.assert_called_once_with("Company", "Acme Inc", "default_holiday_list")

    def test_returns_none_when_no_default_company(self):
        """get_default_holiday_list returns None (no holiday lookup) when no default company is set."""
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.get_single_value", return_value=None),
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.get_value") as mock_value,
        ):
            result = get_default_holiday_list()
        self.assertIsNone(result)
        mock_value.assert_not_called()


class TestGetEmployeesWithBirthday(IntegrationTestCase):
    def test_returns_employee_whose_birthday_is_today(self):
        """get_employees_with_birthday returns the employee whose date_of_birth matches today's day and month."""
        employee = _build_employee(date_of_birth="1990-06-15")
        result = get_employees_with_birthday(MONDAY, employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, MONDAY)

    def test_excludes_employee_whose_birthday_is_not_today(self):
        """get_employees_with_birthday ignores employees whose birthday falls on another day."""
        employee = _build_employee(date_of_birth="1990-06-16")
        result = get_employees_with_birthday(MONDAY, employees=[employee])
        self.assertEqual(result, [])

    def test_excludes_employee_who_opted_out(self):
        """Employees with custom_skip_celebration_announcements set are never announced."""
        employee = _build_employee(date_of_birth="1990-06-15", skip=1)
        result = get_employees_with_birthday(MONDAY, employees=[employee])
        self.assertEqual(result, [])

    def test_excludes_employee_without_date_of_birth(self):
        """Employees with no date_of_birth are skipped rather than raising."""
        employee = _build_employee(date_of_birth=None)
        result = get_employees_with_birthday(MONDAY, employees=[employee])
        self.assertEqual(result, [])

    def test_29_feb_birthday_is_celebrated_on_28_feb_in_a_non_leap_year(self):
        """A 29 Feb birthday is observed on 28 Feb when the year has no 29 Feb."""
        employee = _build_employee(date_of_birth="1992-02-29")
        result = get_employees_with_birthday(date_cls(2025, 2, 28), employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, date_cls(2025, 2, 28))

    def test_29_feb_birthday_is_not_celebrated_on_28_feb_in_a_leap_year(self):
        """In a leap year the 29 Feb birthday is observed on 29 Feb, not 28 Feb."""
        employee = _build_employee(date_of_birth="1992-02-29")
        self.assertEqual(get_employees_with_birthday(date_cls(2028, 2, 28), employees=[employee]), [])
        result = get_employees_with_birthday(date_cls(2028, 2, 29), employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])

    def test_weekend_birthday_is_included_on_monday_with_its_own_date(self):
        """A Saturday birthday is included in Monday's run, carrying the Saturday date as event_date."""
        employee = _build_employee(date_of_birth="1990-06-13")
        result = get_employees_with_birthday(MONDAY, start_date=SATURDAY, employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, SATURDAY)

    def test_29_feb_birthday_rolled_over_a_weekend_is_included_on_monday(self):
        """In non-leap 2027, 28 Feb is a Sunday; the observed 29 Feb birthday is included in Monday 1 Mar's run."""
        monday = date_cls(2027, 3, 1)
        employee = _build_employee(date_of_birth="1992-02-29")
        result = get_employees_with_birthday(monday, start_date=date_cls(2027, 2, 27), employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, date_cls(2027, 2, 28))

    def test_birthday_in_previous_year_is_included_when_window_crosses_new_year(self):
        """A 31 Dec birthday is included in a 1 Jan run whose window starts in the previous year."""
        employee = _build_employee(date_of_birth="1990-12-31")
        result = get_employees_with_birthday(
            date_cls(2029, 1, 1), start_date=date_cls(2028, 12, 30), employees=[employee]
        )
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, date_cls(2028, 12, 31))

    def test_uses_get_active_employees_when_no_employee_list_given(self):
        """Without an explicit employee list, get_employees_with_birthday fetches active employees itself."""
        employee = _build_employee(date_of_birth="1990-06-15")
        with patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=[employee]) as mock_fetch:
            result = get_employees_with_birthday(MONDAY)
        mock_fetch.assert_called_once()
        self.assertEqual([r.name for r in result], ["EMP-0001"])


class TestGetEmployeesWithAnniversary(IntegrationTestCase):
    def test_computes_years_since_joining(self):
        """An employee who joined 3 years ago today gets years == 3."""
        employee = _build_employee(date_of_joining="2023-06-15")
        result = get_employees_with_anniversary(MONDAY, employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].years, 3)
        self.assertEqual(result[0].event_date, MONDAY)

    def test_excludes_employee_who_joined_today(self):
        """An employee whose date_of_joining is today (year == current year) is not an anniversary."""
        employee = _build_employee(date_of_joining="2026-06-15")
        result = get_employees_with_anniversary(MONDAY, employees=[employee])
        self.assertEqual(result, [])

    def test_excludes_employee_who_opted_out(self):
        """Opted-out employees are excluded from anniversary announcements too."""
        employee = _build_employee(date_of_joining="2023-06-15", skip=1)
        result = get_employees_with_anniversary(MONDAY, employees=[employee])
        self.assertEqual(result, [])

    def test_29_feb_joiner_gets_years_counted_on_28_feb(self):
        """Someone who joined on 29 Feb 2024 has a 1-year anniversary observed on 28 Feb 2025."""
        employee = _build_employee(date_of_joining="2024-02-29")
        result = get_employees_with_anniversary(date_cls(2025, 2, 28), employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].years, 1)

    def test_years_are_counted_from_the_event_year_across_new_year(self):
        """A 31 Dec joiner announced in a 1 Jan run gets years based on the event's year, not the run's."""
        employee = _build_employee(date_of_joining="2020-12-31")
        result = get_employees_with_anniversary(
            date_cls(2029, 1, 1), start_date=date_cls(2028, 12, 30), employees=[employee]
        )
        self.assertEqual(result[0].years, 8)

    def test_weekend_anniversary_is_included_on_monday(self):
        """A Sunday anniversary is included in Monday's run with the Sunday date and the right year count."""
        sunday = date_cls(2026, 6, 14)
        employee = _build_employee(date_of_joining="2021-06-14")
        result = get_employees_with_anniversary(MONDAY, start_date=SATURDAY, employees=[employee])
        self.assertEqual([r.name for r in result], ["EMP-0001"])
        self.assertEqual(result[0].event_date, sunday)
        self.assertEqual(result[0].years, 5)


class TestBuildBlocks(IntegrationTestCase):
    def test_birthday_blocks_have_header_and_section_with_mentions(self):
        """build_birthday_blocks renders a header block plus one section whose text contains each employee's mention."""
        employees = [
            {"name": "Alice Example", "mention": f"<@{TEST_SLACK_USER_ID}>", "company": "Acme Inc", "date": MONDAY},
        ]
        blocks = build_birthday_blocks(employees, MONDAY)
        self.assertEqual(blocks[0]["type"], "header")
        text = _section_text(blocks)
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)

    def test_default_birthday_template_groups_everyone_in_one_message(self):
        """The default birthday template lists every employee in a single section."""
        employees = [
            {"name": "Alice Example", "mention": f"<@{TEST_SLACK_USER_ID}>", "company": "Acme Inc", "date": MONDAY},
            {"name": "Bob Example", "mention": "Bob Example", "company": "Acme Inc", "date": MONDAY},
        ]
        text = _section_text(build_birthday_blocks(employees, MONDAY))
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)
        self.assertIn("Bob Example", text)

    def test_default_birthday_template_names_the_day_for_rolled_back_events(self):
        """When an employee's event date differs from today, the default template says which day it was for."""
        employees = [
            {"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": SATURDAY},
        ]
        text = _section_text(build_birthday_blocks(employees, MONDAY))
        self.assertIn("Saturday", text)

    def test_custom_birthday_template_is_used_when_set(self):
        """A custom Jinja template from Slack Settings replaces the default birthday wording."""
        employees = [{"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": MONDAY}]
        template = "Cake time for {{ employees | map(attribute='name') | join(' & ') }}!"
        text = _section_text(build_birthday_blocks(employees, MONDAY, template=template))
        self.assertEqual(text, "Cake time for Alice Example!")

    def test_default_anniversary_template_shows_years_and_company(self):
        """The default anniversary template includes the year count and the employee's company."""
        employees = [
            {
                "name": "Alice Example",
                "mention": f"<@{TEST_SLACK_USER_ID}>",
                "company": "Acme Inc",
                "date": MONDAY,
                "years": 3,
            },
        ]
        text = _section_text(build_anniversary_blocks(employees, MONDAY))
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)
        self.assertIn("3 years", text)
        self.assertIn("Acme Inc", text)

    def test_default_anniversary_template_uses_singular_for_one_year(self):
        """One year of service renders as '1 year', not '1 years'."""
        employees = [
            {"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": MONDAY, "years": 1}
        ]
        text = _section_text(build_anniversary_blocks(employees, MONDAY))
        self.assertIn("1 year", text)
        self.assertNotIn("1 years", text)

    def test_custom_anniversary_template_is_used_when_set(self):
        """A custom Jinja template from Slack Settings replaces the default anniversary wording."""
        employees = [
            {"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": MONDAY, "years": 2}
        ]
        template = "{% for e in employees %}{{ e.name }}: {{ e.years }}y{% endfor %}"
        text = _section_text(build_anniversary_blocks(employees, MONDAY, template=template))
        self.assertEqual(text, "Alice Example: 2y")


class TestSplitTextAndChunking(IntegrationTestCase):
    def test_split_text_keeps_short_text_in_one_chunk(self):
        """split_text returns the whole text as one chunk when it fits the limit."""
        self.assertEqual(split_text("a\nb", limit=10), ["a\nb"])

    def test_split_text_breaks_on_line_boundaries(self):
        """split_text splits between lines rather than mid-line when a line boundary is available."""
        self.assertEqual(split_text("aaaa\nbbbb\ncccc", limit=9), ["aaaa\nbbbb", "cccc"])

    def test_split_text_hard_splits_an_overlong_line(self):
        """A single comma-free line longer than the limit is cut into limit-sized pieces."""
        self.assertEqual(split_text("x" * 25, limit=10), ["x" * 10, "x" * 10, "x" * 5])

    def test_split_text_breaks_an_overlong_line_on_comma_separators(self):
        """An over-long line of comma-separated mentions is split between items, never inside a <@U...> token."""
        mentions = [f"<@U{i:06d}>" for i in range(50)]  # 10 chars each, 12 with ", "
        chunks = split_text(", ".join(mentions), limit=50)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 50)
            self.assertEqual(chunk.count("<@"), chunk.count(">"))
            self.assertTrue(chunk.startswith("<@") and chunk.endswith(">"))
        self.assertEqual(", ".join(chunks), ", ".join(mentions))

    def test_split_text_does_not_cut_an_entity(self):
        """Escaped names like 'Tom &amp; Co' in a comma list are kept whole across chunks."""
        items = ["Tom &amp; Co"] * 6
        chunks = split_text(", ".join(items), limit=30)
        self.assertEqual(", ".join(chunks), ", ".join(items))
        for chunk in chunks:
            self.assertEqual(chunk.count("&amp;"), chunk.count("Tom"))

    def test_describe_event_day_uses_weekday_for_recent_events(self):
        """Events up to 6 days before the run are described by weekday name."""
        self.assertEqual(describe_event_day(SATURDAY, MONDAY), "Saturday")
        self.assertEqual(describe_event_day(date_cls(2026, 6, 9), MONDAY), "Tuesday")

    def test_describe_event_day_uses_date_for_older_events(self):
        """Events more than 6 days before the run are described by a short date, since a weekday would be ambiguous."""
        self.assertEqual(describe_event_day(date_cls(2026, 6, 8), MONDAY), "Jun 8")
        self.assertEqual(describe_event_day(date_cls(2026, 10, 1), date_cls(2026, 10, 9)), "Oct 1")

    def test_default_template_shows_date_for_events_older_than_a_week(self):
        """A caught-up event from 7 days ago renders '(for Jun 8)' rather than a weekday name."""
        employees = [
            {"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": date_cls(2026, 6, 8)}
        ]
        text = _section_text(build_birthday_blocks(employees, MONDAY))
        self.assertIn("(for Jun 8)", text)

    def test_long_render_is_chunked_into_multiple_sections(self):
        """A rendered message over Slack's 3000-char section limit becomes several section blocks, each within the limit."""
        employees = [
            {"name": f"Employee {i:03d}", "mention": f"Employee {i:03d}", "company": "Acme Inc", "date": MONDAY}
            for i in range(400)
        ]
        template = "{% for e in employees %}{{ e.name }}\n{% endfor %}"
        blocks = build_birthday_blocks(employees, MONDAY, template=template)
        self.assertEqual(blocks[0]["type"], "header")
        sections = [b for b in blocks if b["type"] == "section"]
        self.assertGreater(len(sections), 1)
        for section in sections:
            self.assertLessEqual(len(section["text"]["text"]), SLACK_SECTION_TEXT_LIMIT)
        self.assertEqual("\n".join(s["text"]["text"] for s in sections).count("Employee "), 400)

    def test_empty_render_returns_no_blocks(self):
        """A template that renders to whitespace yields no blocks at all (so nothing is posted)."""
        employees = [{"name": "Alice Example", "mention": "Alice Example", "company": "Acme Inc", "date": MONDAY}]
        self.assertEqual(build_birthday_blocks(employees, MONDAY, template="{% if false %}x{% endif %}  "), [])


class TestSendCelebrations(IntegrationTestCase):
    def _run(
        self,
        settings,
        employees,
        *,
        slack=None,
        slack_ids=None,
        holiday_list=None,
        date="2026-06-15",
        previous_run=None,
        slack_class=None,
    ):
        slack = slack or _build_slack_mock()
        slack_class = slack_class or MagicMock(return_value=slack)
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.claim_celebrations_day", return_value=True),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", slack_class),
            patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=employees),
            patch(f"{CELEBRATIONS_MODULE}.get_slack_user_ids", return_value=slack_ids or {}),
            patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=holiday_list),
            patch(f"{CELEBRATIONS_MODULE}.is_holiday", return_value=False),
        ):
            send_celebrations(date, previous_run=previous_run)
        return slack

    def test_posts_nothing_when_no_events(self):
        """send_celebrations does not call chat.postMessage, nor even construct SlackIntegration, when nobody has an event."""
        slack_class = MagicMock(return_value=_build_slack_mock())
        slack = self._run(
            _build_settings_mock(), [_build_employee(date_of_birth="1990-01-01")], slack_class=slack_class
        )
        slack.slack_app.client.chat_postMessage.assert_not_called()
        slack_class.assert_not_called()

    def _run_with_real_claim(self, settings, employees, *, claimed, slack_class, set_mock=None):
        set_patch, make_key_patch = _patch_cache_claim(claimed, set_mock=set_mock)
        with (
            set_patch as mock_set,
            make_key_patch as mock_make_key,
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", slack_class),
            patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=employees),
            patch(f"{CELEBRATIONS_MODULE}.get_slack_user_ids", return_value={}),
            patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=None),
        ):
            send_celebrations("2026-06-15")
        return mock_set, mock_make_key

    def test_claims_the_day_in_redis_before_posting(self):
        """send_celebrations claims the run date with SET NX and a 2-day TTL after the channel resolves and before the first post."""
        order = []
        slack = _build_slack_mock()
        slack.slack_app.client.chat_postMessage.side_effect = lambda **kwargs: order.append("post") or {"ok": True}
        set_mock = MagicMock(side_effect=lambda *args, **kwargs: order.append("claim") or True)
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        mock_set, mock_make_key = self._run_with_real_claim(
            settings, [employee], claimed=True, slack_class=MagicMock(return_value=slack), set_mock=set_mock
        )
        # Frappe internals (meta, singles) also call make_key, so only pin our call
        mock_make_key.assert_any_call("fsc_celebrations_posted::2026-06-15")
        mock_set.assert_called_once_with("test|fsc_celebrations_posted::2026-06-15", 1, nx=True, ex=2 * 86400)
        self.assertEqual(order, ["claim", "post"])

    def test_does_not_claim_the_day_when_nothing_to_post(self):
        """With no events, the day is not claimed, so a later (corrected) run for the same date can still post."""
        settings = _build_settings_mock()
        with patch(f"{CELEBRATIONS_MODULE}.claim_celebrations_day") as mock_claim:
            with (
                patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
                patch(f"{CELEBRATIONS_MODULE}.SlackIntegration"),
                patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=[]),
                patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=None),
            ):
                send_celebrations("2026-06-15")
        mock_claim.assert_not_called()

    def test_does_not_claim_the_day_when_no_channel_configured(self):
        """A missing channel logs and returns before the claim, so fixing the channel and re-running the day still posts."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0, celebrations_channel_id="")
        slack = _build_slack_mock()
        slack.SLACK_CHANNEL_ID = None
        with (
            patch(f"{CELEBRATIONS_MODULE}.claim_celebrations_day") as mock_claim,
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log"),
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=slack),
            patch(f"{CELEBRATIONS_MODULE}.get_active_employees", return_value=[employee]),
            patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=None),
        ):
            send_celebrations("2026-06-15")
        mock_claim.assert_not_called()

    def test_second_job_for_the_same_date_does_not_post(self):
        """If the day was already claimed (duplicate job for the same date), nothing is posted and an info line is logged."""
        slack = _build_slack_mock()
        slack_class = MagicMock(return_value=slack)
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.logger") as mock_logger,
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log,
        ):
            self._run_with_real_claim(settings, [employee], claimed=False, slack_class=slack_class)
        slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_logger.return_value.info.assert_called_once()
        mock_log.assert_not_called()

    def test_claim_celebrations_day_returns_false_when_key_exists(self):
        """claim_celebrations_day maps the redis SET NX result to a bool."""
        set_patch, make_key_patch = _patch_cache_claim(claimed=None)
        with set_patch, make_key_patch:
            self.assertFalse(claim_celebrations_day(MONDAY))
        set_patch, make_key_patch = _patch_cache_claim(claimed=True)
        with set_patch, make_key_patch:
            self.assertTrue(claim_celebrations_day(MONDAY))

    def test_inactive_employees_are_not_announced(self):
        """An inactive employee with a birthday today is not announced: the Employee query filters on status=Active."""
        rows = [
            _build_employee(name="EMP-0001", employee_name="Alice Example", user_id="alice@example.com"),
            _build_employee(name="EMP-0002", employee_name="Bob Example", user_id="bob@example.com"),
        ]
        rows[0].status = "Active"
        rows[1].status = "Left"

        def fake_get_all(doctype, filters=None, fields=None, **kwargs):
            if doctype == "Employee":
                return [frappe._dict(r) for r in rows if r.status == filters.get("status")]
            return []

        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = _build_slack_mock()
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.claim_celebrations_day", return_value=True),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=slack),
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_meta", return_value=_build_employee_meta()),
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_all", side_effect=fake_get_all),
            patch(f"{CELEBRATIONS_MODULE}.get_default_holiday_list", return_value=None),
        ):
            send_celebrations("2026-06-15")
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example", text)
        self.assertNotIn("Bob Example", text)

    def test_single_birthday_posts_one_message_with_mention(self):
        """One birthday today produces exactly one post, mentioning the employee by Slack ID."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee], slack_ids={"alice@example.com": TEST_SLACK_USER_ID})
        slack.slack_app.client.chat_postMessage.assert_called_once()
        kwargs = slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C0CELEB001")
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", _section_text(kwargs["blocks"]))

    def test_two_birthdays_are_grouped_in_one_message(self):
        """Two employees sharing a birthday are announced together in a single post."""
        employees = [
            _build_employee(name="EMP-0001", employee_name="Alice Example", user_id="alice@example.com"),
            _build_employee(name="EMP-0002", employee_name="Bob Example", user_id="bob@example.com"),
        ]
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(
            settings,
            employees,
            slack_ids={"alice@example.com": TEST_SLACK_USER_ID, "bob@example.com": TEST_SLACK_USER_ID_2},
        )
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)
        self.assertIn(f"<@{TEST_SLACK_USER_ID_2}>", text)

    def test_falls_back_to_name_when_mention_disabled(self):
        """With mention_user off, employees are shown by name even when a Slack ID is known."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0, mention_user=0)
        slack = self._run(settings, [employee], slack_ids={"alice@example.com": TEST_SLACK_USER_ID})
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example", text)
        self.assertNotIn(f"<@{TEST_SLACK_USER_ID}>", text)

    def test_falls_back_to_name_when_no_slack_id(self):
        """Employees without a Slack ID are shown by name even when mention_user is on."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee], slack_ids={})
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example", text)

    def test_falls_back_to_attendance_channel_when_celebrations_channel_empty(self):
        """An empty celebrations_channel_id posts to the attendance channel (SlackIntegration.SLACK_CHANNEL_ID)."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0, celebrations_channel_id="")
        slack = self._run(settings, [employee])
        kwargs = slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], TEST_SLACK_CHANNEL_ID)

    def test_logs_error_and_skips_post_when_no_channel_configured(self):
        """With neither celebrations_channel_id nor an attendance channel set, the post is skipped and an error logged."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0, celebrations_channel_id="")
        slack = _build_slack_mock()
        slack.SLACK_CHANNEL_ID = None
        with patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log:
            self._run(settings, [employee], slack=slack)
        slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_passes_plain_text_fallback_to_slack(self):
        """chat.postMessage gets a plain `text` fallback naming the employees alongside the blocks."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee])
        kwargs = slack.slack_app.client.chat_postMessage.call_args.kwargs
        self.assertIn("Alice Example", kwargs["text"])

    def test_escapes_slack_control_characters_in_names_and_company(self):
        """&, < and > in employee_name and company are escaped so they cannot be read as mentions or links."""
        employee = _build_employee(
            employee_name="Tom & <Jerry>", company="A&B <Co>", date_of_birth="1990-06-15", date_of_joining="2021-06-15"
        )
        settings = _build_settings_mock(mention_user=0)
        slack = self._run(settings, [employee])
        texts = [_section_text(c.kwargs["blocks"]) for c in slack.slack_app.client.chat_postMessage.call_args_list]
        self.assertEqual(len(texts), 2)
        for text in texts:
            self.assertIn("Tom &amp; &lt;Jerry&gt;", text)
            self.assertNotIn("<Jerry>", text)
        self.assertTrue(any("A&amp;B &lt;Co&gt;" in t for t in texts))

    def test_mentions_are_not_escaped(self):
        """A real Slack mention stays raw (<@U...>) while the name is escaped."""
        employee = _build_employee(employee_name="Tom & Co", date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee], slack_ids={"alice@example.com": TEST_SLACK_USER_ID})
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)

    def test_two_anniversaries_are_grouped_in_one_message(self):
        """Two employees sharing an anniversary date are announced together in a single post with their own years."""
        employees = [
            _build_employee(name="EMP-0001", employee_name="Alice Example", date_of_joining="2023-06-15"),
            _build_employee(name="EMP-0002", employee_name="Bob Example", date_of_joining="2016-06-15"),
        ]
        settings = _build_settings_mock(send_birthday_updates=0, mention_user=0)
        slack = self._run(settings, employees)
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example - 3 years", text)
        self.assertIn("Bob Example - 10 years", text)

    def test_catches_up_events_missed_since_previous_run(self):
        """With previous_run three working days ago, a birthday on one of the skipped days is still announced."""
        employee = _build_employee(date_of_birth="1990-06-16")  # Tuesday
        settings = _build_settings_mock(send_anniversary_updates=0, mention_user=0)
        slack = self._run(settings, [employee], date="2026-06-18", previous_run="2026-06-15")
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example (for Tuesday)", text)

    def test_empty_render_skips_the_post(self):
        """A template that renders to nothing results in no chat.postMessage call and no error."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(
            send_anniversary_updates=0, birthday_message_template="{% if false %}x{% endif %}"
        )
        with patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log:
            slack = self._run(settings, [employee])
        slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_not_called()

    def test_broken_birthday_render_is_logged_and_anniversary_still_posted(self):
        """If building the birthday message raises, the error is logged once and the anniversary post still goes out."""
        employee = _build_employee(date_of_birth="1990-06-15", date_of_joining="2021-06-15")
        settings = _build_settings_mock(mention_user=0)
        with (
            patch(f"{CELEBRATIONS_MODULE}.build_birthday_blocks", side_effect=RuntimeError("bad template")),
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log,
        ):
            slack = self._run(settings, [employee])
        mock_log.assert_called_once()
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("5 years", text)

    def test_opted_out_employee_is_not_announced(self):
        """An employee with custom_skip_celebration_announcements=1 produces no post for either event type."""
        employee = _build_employee(date_of_birth="1990-06-15", date_of_joining="2020-06-15", skip=1)
        slack = self._run(_build_settings_mock(), [employee])
        slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_birthday_and_anniversary_post_separately(self):
        """One employee with both events today triggers one birthday post and one anniversary post."""
        employee = _build_employee(date_of_birth="1990-06-15", date_of_joining="2022-06-15")
        slack = self._run(_build_settings_mock(), [employee])
        self.assertEqual(slack.slack_app.client.chat_postMessage.call_count, 2)
        texts = [_section_text(c.kwargs["blocks"]) for c in slack.slack_app.client.chat_postMessage.call_args_list]
        self.assertTrue(any("4 years" in t for t in texts))

    def test_anniversary_disabled_skips_anniversary_post(self):
        """With send_anniversary_updates=0, an anniversary today is not posted."""
        employee = _build_employee(date_of_birth="1990-01-01", date_of_joining="2022-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee])
        slack.slack_app.client.chat_postMessage.assert_not_called()

    def test_monday_run_includes_saturday_birthday(self):
        """A Saturday birthday is posted on Monday and the message names Saturday."""
        employee = _build_employee(date_of_birth="1990-06-13")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = self._run(settings, [employee])
        slack.slack_app.client.chat_postMessage.assert_called_once()
        text = _section_text(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"])
        self.assertIn("Alice Example", text)
        self.assertIn("Saturday", text)

    def test_custom_templates_from_settings_are_used(self):
        """Templates stored in Slack Settings are rendered instead of the defaults."""
        employee = _build_employee(date_of_birth="1990-06-15", date_of_joining="2021-06-15")
        settings = _build_settings_mock(
            birthday_message_template="BDAY {{ employees[0].name }}",
            anniversary_message_template="ANNIV {{ employees[0].years }}",
        )
        slack = self._run(settings, [employee])
        texts = [_section_text(c.kwargs["blocks"]) for c in slack.slack_app.client.chat_postMessage.call_args_list]
        self.assertIn("BDAY Alice Example", texts)
        self.assertIn("ANNIV 5", texts)

    def test_birth_year_never_appears_in_rendered_message(self):
        """Neither the default birthday nor anniversary message contains the birth year."""
        employee = _build_employee(date_of_birth="1990-06-15", date_of_joining="2021-06-15")
        slack = self._run(_build_settings_mock(), [employee])
        for call in slack.slack_app.client.chat_postMessage.call_args_list:
            for block in call.kwargs["blocks"]:
                self.assertNotIn("1990", block["text"]["text"])

    def test_logs_error_and_does_not_raise_when_slack_post_fails(self):
        """A chat.postMessage failure is logged via generate_error_log and does not propagate."""
        employee = _build_employee(date_of_birth="1990-06-15")
        settings = _build_settings_mock(send_anniversary_updates=0)
        slack = _build_slack_mock(post_side_effect=RuntimeError("channel_not_found"))
        with patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log:
            self._run(settings, [employee], slack=slack)
        mock_log.assert_called_once()


class TestSlackSettingsTemplateValidation(IntegrationTestCase):
    def test_template_fields_are_code_fields(self):
        """Both template fields are Code (Jinja) fields in the doctype JSON on disk, which are exempt from the HTML sanitizer that would break Jinja."""
        path = frappe.get_app_path(
            "frappe_slack_connector", "frappe_slack_connector", "doctype", "slack_settings", "slack_settings.json"
        )
        with open(path) as f:
            fields = {field["fieldname"]: field for field in json.load(f)["fields"]}
        for fieldname in ("birthday_message_template", "anniversary_message_template"):
            self.assertIn(fieldname, fields)
            self.assertEqual(fields[fieldname]["fieldtype"], "Code", fieldname)
            self.assertEqual(fields[fieldname]["options"], "Jinja", fieldname)

    def test_rejects_template_with_jinja_syntax_error(self):
        """validate_celebration_templates raises a ValidationError for a syntactically invalid template."""
        doc = frappe._dict(birthday_message_template="{% if %}", anniversary_message_template=None)
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(doc)

    def test_real_document_validate_rejects_bad_template(self):
        """doc.validate() on the actual Slack Settings document raises for a bad anniversary template."""
        doc = frappe.get_single("Slack Settings")
        doc.anniversary_message_template = "{{ employees"
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_accepts_valid_and_empty_templates(self):
        """Valid Jinja and empty templates pass validation."""
        doc = frappe._dict(birthday_message_template="Hi {{ employees | length }}", anniversary_message_template="")
        SlackSettings.validate_celebration_templates(doc)


class TestSlackSettingsResetOnEnable(IntegrationTestCase):
    def _doc(self, *, birthday, anniversary, before, last=None):
        doc = frappe._dict(
            send_birthday_updates=birthday,
            send_anniversary_updates=anniversary,
            last_celebrations_date=last,
            get_doc_before_save=lambda: before,
        )
        return doc

    def test_sets_last_date_to_yesterday_when_birthday_updates_turned_on(self):
        """Turning birthday updates on resets last_celebrations_date to yesterday so only today is announced."""
        before = frappe._dict(send_birthday_updates=0, send_anniversary_updates=0)
        doc = self._doc(birthday=1, anniversary=0, before=before, last=date_cls(2026, 1, 1))
        with patch(f"{SLACK_SETTINGS_MODULE}.nowdate", return_value="2026-06-15"):
            self.assertTrue(SlackSettings.reset_celebrations_date_on_enable(doc))
        self.assertEqual(frappe.utils.getdate(doc.last_celebrations_date), date_cls(2026, 6, 14))

    def test_sets_last_date_when_anniversary_updates_turned_on(self):
        """Turning anniversary updates on (birthday already on) also resets the date."""
        before = frappe._dict(send_birthday_updates=1, send_anniversary_updates=0)
        doc = self._doc(birthday=1, anniversary=1, before=before, last=date_cls(2026, 1, 1))
        with patch(f"{SLACK_SETTINGS_MODULE}.nowdate", return_value="2026-06-15"):
            SlackSettings.reset_celebrations_date_on_enable(doc)
        self.assertEqual(frappe.utils.getdate(doc.last_celebrations_date), date_cls(2026, 6, 14))

    def test_leaves_last_date_alone_when_toggles_unchanged(self):
        """Saving with the toggles unchanged (e.g. editing a template) keeps last_celebrations_date."""
        before = frappe._dict(send_birthday_updates=1, send_anniversary_updates=1)
        doc = self._doc(birthday=1, anniversary=1, before=before, last=date_cls(2026, 6, 10))
        self.assertFalse(SlackSettings.reset_celebrations_date_on_enable(doc))
        self.assertEqual(doc.last_celebrations_date, date_cls(2026, 6, 10))

    def test_leaves_last_date_alone_when_turned_off(self):
        """Turning a toggle off does not touch last_celebrations_date."""
        before = frappe._dict(send_birthday_updates=1, send_anniversary_updates=0)
        doc = self._doc(birthday=0, anniversary=0, before=before, last=date_cls(2026, 6, 10))
        SlackSettings.reset_celebrations_date_on_enable(doc)
        self.assertEqual(doc.last_celebrations_date, date_cls(2026, 6, 10))

    def test_treats_missing_doc_before_save_as_off(self):
        """Without a doc_before_save (first save), an enabled toggle counts as newly enabled."""
        doc = self._doc(birthday=1, anniversary=0, before=None)
        with patch(f"{SLACK_SETTINGS_MODULE}.nowdate", return_value="2026-06-15"):
            SlackSettings.reset_celebrations_date_on_enable(doc)
        self.assertEqual(frappe.utils.getdate(doc.last_celebrations_date), date_cls(2026, 6, 14))


class TestSlackSettingsKeepLatestDate(IntegrationTestCase):
    def _keep(self, *, incoming, stored):
        doc = frappe._dict(last_celebrations_date=incoming)
        with patch(f"{SLACK_SETTINGS_MODULE}.frappe.db.get_single_value", return_value=stored) as mock_stored:
            SlackSettings.keep_latest_celebrations_date(doc)
        mock_stored.assert_called_once_with("Slack Settings", "last_celebrations_date")
        return doc.get("last_celebrations_date")

    def test_keeps_stored_date_when_it_is_later_than_the_incoming_one(self):
        """A form loaded before today's post (incoming 14 Jun) must not overwrite the stamp the scheduler wrote (15 Jun)."""
        result = self._keep(incoming=date_cls(2026, 6, 14), stored="2026-06-15")
        self.assertEqual(frappe.utils.getdate(result), date_cls(2026, 6, 15))

    def test_keeps_incoming_date_when_it_is_later(self):
        """An incoming date later than the stored one is kept as is."""
        result = self._keep(incoming=date_cls(2026, 6, 16), stored="2026-06-15")
        self.assertEqual(result, date_cls(2026, 6, 16))

    def test_uses_stored_date_when_incoming_is_empty(self):
        """An empty incoming date is replaced by the stored one."""
        result = self._keep(incoming=None, stored="2026-06-15")
        self.assertEqual(frappe.utils.getdate(result), date_cls(2026, 6, 15))

    def test_leaves_incoming_when_nothing_stored(self):
        """With nothing stored in the DB the incoming value is untouched."""
        result = self._keep(incoming=date_cls(2026, 6, 14), stored=None)
        self.assertEqual(result, date_cls(2026, 6, 14))

    def test_real_document_validate_keeps_later_stored_date(self):
        """doc.validate() on the actual Slack Settings document (toggles unchanged) restores a later stored date."""
        doc = frappe.get_single("Slack Settings")
        doc.send_birthday_updates = 0
        doc.send_anniversary_updates = 0
        doc.birthday_message_template = None
        doc.anniversary_message_template = None
        doc.last_celebrations_date = date_cls(2026, 6, 14)
        with patch(f"{SLACK_SETTINGS_MODULE}.frappe.db.get_single_value", return_value="2026-06-15"):
            doc.validate()
        self.assertEqual(frappe.utils.getdate(doc.last_celebrations_date), date_cls(2026, 6, 15))

    def test_validate_does_not_override_a_toggle_reset(self):
        """When a toggle was just switched on, validate keeps the yesterday reset even if the stored date is later."""
        doc = frappe.get_single("Slack Settings")
        doc.send_birthday_updates = 1
        doc.send_anniversary_updates = 0
        doc.birthday_message_template = None
        doc.anniversary_message_template = None
        doc._doc_before_save = frappe._dict(send_birthday_updates=0, send_anniversary_updates=0)
        with (
            patch(f"{SLACK_SETTINGS_MODULE}.nowdate", return_value="2026-06-15"),
            patch(f"{SLACK_SETTINGS_MODULE}.frappe.db.get_single_value", return_value="2026-06-15") as mock_stored,
        ):
            doc.validate()
        self.assertEqual(frappe.utils.getdate(doc.last_celebrations_date), date_cls(2026, 6, 14))
        mock_stored.assert_not_called()

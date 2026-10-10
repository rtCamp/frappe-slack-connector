from datetime import date, time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.tasks.celebrations import (
    celebrations_channel,
    get_employees_with_event,
    get_slack_user_ids,
    send_celebrations,
)
from frappe_slack_connector.tests import (
    TEST_SLACK_CHANNEL_ID,
    TEST_SLACK_USER_ID,
    TEST_SLACK_USER_ID_2,
    TEST_USER,
    make_test_user,
    make_test_user_meta,
)

CELEBRATIONS_MODULE = "frappe_slack_connector.tasks.celebrations"
TODAY = "2026-06-15"
CELEBRATIONS_CHANNEL_ID = "C0FSC0003"


def _build_settings_mock(
    *,
    send_celebration_updates=1,
    last_celebrations_date=None,
    celebrations_time="09:00:00",
    celebrations_channel_id=CELEBRATIONS_CHANNEL_ID,
    mention_user=1,
    birthday_message_template=None,
    anniversary_message_template=None,
):
    """Build a MagicMock that mimics the Slack Settings Single doc with the celebrations fields."""
    settings = MagicMock()
    settings.send_celebration_updates = send_celebration_updates
    settings.last_celebrations_date = last_celebrations_date
    settings.celebrations_time = celebrations_time
    settings.celebrations_channel_id = celebrations_channel_id
    settings.mention_user = mention_user
    settings.birthday_message_template = birthday_message_template
    settings.anniversary_message_template = anniversary_message_template
    return settings


def _employee(name, *, user_id=None, company="Example Co", date_of_joining=date(2023, 6, 15)):
    """Build a row shaped like HRMS's get_employees_having_an_event_today output."""
    return frappe._dict(
        name=name,
        user_id=user_id,
        company=company,
        date_of_joining=date_of_joining,
        personal_email=None,
        company_email=None,
        image=None,
    )


def _grouped(*employees):
    """Group rows by company the way HRMS returns them."""
    grouped = {}
    for employee in employees:
        grouped.setdefault(employee.company, []).append(employee)
    return grouped


class TestCelebrationsChannel(IntegrationTestCase):
    def _run(self, settings, *, now=time(10, 0)):
        """Run celebrations_channel with the clock, post and stamp mocked; returns (mock_send, mock_stamp)."""
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=TODAY),
            patch(
                f"{CELEBRATIONS_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=now)),
            ),
            patch(f"{CELEBRATIONS_MODULE}.send_celebrations") as mock_send,
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.set_single_value") as mock_stamp,
        ):
            celebrations_channel()
        return mock_send, mock_stamp

    def test_returns_silently_when_celebration_updates_disabled(self):
        """celebrations_channel does nothing when Slack Settings.send_celebration_updates=0."""
        settings = _build_settings_mock(send_celebration_updates=0)
        mock_send, mock_stamp = self._run(settings)
        mock_send.assert_not_called()
        mock_stamp.assert_not_called()

    def test_returns_silently_when_already_run_today(self):
        """celebrations_channel does nothing when last_celebrations_date is today (posts once a day)."""
        settings = _build_settings_mock(last_celebrations_date=date(2026, 6, 15))
        mock_send, mock_stamp = self._run(settings)
        mock_send.assert_not_called()
        mock_stamp.assert_not_called()

    def test_returns_silently_before_celebrations_time(self):
        """celebrations_channel neither posts nor stamps when the current time is before celebrations_time."""
        settings = _build_settings_mock(celebrations_time="09:00:00")
        mock_send, mock_stamp = self._run(settings, now=time(8, 30))
        mock_send.assert_not_called()
        mock_stamp.assert_not_called()

    def test_treats_missing_celebrations_time_as_nine(self):
        """With celebrations_time unset the job runs after 09:00 and not before."""
        mock_send, _ = self._run(_build_settings_mock(celebrations_time=None), now=time(8, 59))
        mock_send.assert_not_called()
        mock_send, _ = self._run(_build_settings_mock(celebrations_time=None), now=time(9, 0))
        mock_send.assert_called_once()

    def test_posts_and_stamps_date_when_conditions_met(self):
        """When all guards pass, celebrations_channel calls send_celebrations and stamps today's date directly in the DB, without a full save."""
        settings = _build_settings_mock(last_celebrations_date=date(2026, 6, 14))
        mock_send, mock_stamp = self._run(settings)
        mock_send.assert_called_once()
        mock_stamp.assert_called_once_with("Slack Settings", "last_celebrations_date", TODAY)
        settings.save.assert_not_called()

    def test_does_not_stamp_when_send_celebrations_raises(self):
        """If send_celebrations raises, the exception propagates and the date is not stamped, so the next tick retries."""
        settings = _build_settings_mock()
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=TODAY),
            patch(
                f"{CELEBRATIONS_MODULE}.frappe.utils.now_datetime",
                return_value=MagicMock(time=MagicMock(return_value=time(10, 0))),
            ),
            patch(f"{CELEBRATIONS_MODULE}.send_celebrations", side_effect=RuntimeError("boom")),
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.set_single_value") as mock_stamp,
            self.assertRaises(RuntimeError),
        ):
            celebrations_channel()
        mock_stamp.assert_not_called()


class TestSendCelebrations(IntegrationTestCase):
    def _run(self, settings, *, birthdays=(), anniversaries=(), slack_user_ids=None, templates=None):
        """Run send_celebrations with HRMS, User Meta, Slack and the Email Template lookup mocked; returns (mock_slack, mock_log).

        ``templates`` maps an Email Template name to its Jinja source (a str, stored with Use HTML on) or to a
        dict of Email Template columns; a name that is not in it does not exist.
        """
        events = {"birthday": _grouped(*birthdays), "work_anniversary": _grouped(*anniversaries)}
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = TEST_SLACK_CHANNEL_ID

        original_get_value = frappe.db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype != "Email Template":
                return original_get_value(doctype, *args, **kwargs)
            name = args[0] if args else kwargs.get("filters")
            fieldname = args[1] if len(args) > 1 else kwargs.get("fieldname")
            if templates is None:
                self.fail(f"Email Template {name!r} looked up although nothing is linked")
            self.assertEqual(fieldname, ["use_html", "response_html"])
            template = (templates or {}).get(name)
            if template is None:
                return None
            if isinstance(template, str):
                template = {"use_html": 1, "response_html": template}
            return frappe._dict(template)

        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{CELEBRATIONS_MODULE}.frappe.db.get_value", side_effect=get_value),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=TODAY),
            patch(f"{CELEBRATIONS_MODULE}.get_employees_having_an_event_today", side_effect=events.get),
            patch(f"{CELEBRATIONS_MODULE}.get_slack_user_ids", return_value=slack_user_ids or {}),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log,
        ):
            send_celebrations()
        return mock_slack, mock_log

    def _section_text(self, call):
        return call.kwargs["blocks"][1]["text"]["text"]

    def test_posts_nothing_when_nobody_has_an_event(self):
        """No Slack message is posted when nobody has a birthday or anniversary today."""
        mock_slack, mock_log = self._run(_build_settings_mock())
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_not_called()

    def test_posts_one_birthday_message_with_mention(self):
        """One birthday post goes to the celebrations channel, mentioning the employee."""
        alice = _employee("Alice Example", user_id=TEST_USER)
        mock_slack, _ = self._run(
            _build_settings_mock(), birthdays=[alice], slack_user_ids={TEST_USER: TEST_SLACK_USER_ID}
        )
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        call = mock_slack.slack_app.client.chat_postMessage.call_args
        self.assertEqual(call.kwargs["channel"], CELEBRATIONS_CHANNEL_ID)
        self.assertEqual(call.kwargs["blocks"][0]["text"]["text"], ":birthday: Birthdays")
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", self._section_text(call))
        self.assertIn("Alice Example", call.kwargs["text"])

    def test_groups_several_people_in_one_message(self):
        """Everyone sharing the day is listed in a single post."""
        alice = _employee("Alice Example", user_id="alice@example.com")
        bob = _employee("Bob Example", user_id="bob@example.com", company="Other Co")
        mock_slack, _ = self._run(
            _build_settings_mock(),
            birthdays=[alice, bob],
            slack_user_ids={"alice@example.com": TEST_SLACK_USER_ID, "bob@example.com": TEST_SLACK_USER_ID_2},
        )
        mock_slack.slack_app.client.chat_postMessage.assert_called_once()
        text = self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args)
        self.assertIn(f"• <@{TEST_SLACK_USER_ID}>\n• <@{TEST_SLACK_USER_ID_2}>", text)

    def test_mentions_even_when_mention_user_is_off(self):
        """The attendance summary's mention_user setting does not apply: an employee with a Slack ID is mentioned regardless, since the event comes once a year."""
        alice = _employee("Alice Example", user_id=TEST_USER)
        mock_slack, _ = self._run(
            _build_settings_mock(mention_user=0), birthdays=[alice], slack_user_ids={TEST_USER: TEST_SLACK_USER_ID}
        )
        text = self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args)
        self.assertIn(f"<@{TEST_SLACK_USER_ID}>", text)
        self.assertNotIn("Alice Example", text)

    def test_shows_name_when_employee_has_no_slack_id(self):
        """An employee without a Slack ID is shown by name."""
        alice = _employee("Alice Example", user_id=TEST_USER)
        mock_slack, _ = self._run(_build_settings_mock(), birthdays=[alice])
        self.assertIn("Alice Example", self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args))

    def test_escapes_slack_control_characters_in_names(self):
        """& < > in an employee name or company are escaped so they cannot be read as mentions or links."""
        tom = _employee("Tom & <Jerry>", company="A <B>")
        mock_slack, _ = self._run(_build_settings_mock(), anniversaries=[tom])
        text = self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args)
        self.assertIn("Tom &amp; &lt;Jerry&gt;", text)
        self.assertIn("*A &lt;B&gt;*", text)

    def test_anniversary_message_shows_years_and_company(self):
        """The anniversary post lists the employee with the number of completed years under the company name."""
        alice = _employee("Alice Example", date_of_joining=date(2023, 6, 15))
        mock_slack, _ = self._run(_build_settings_mock(), anniversaries=[alice])
        call = mock_slack.slack_app.client.chat_postMessage.call_args
        self.assertEqual(call.kwargs["blocks"][0]["text"]["text"], ":tada: Work Anniversaries")
        self.assertEqual(
            self._section_text(call),
            ":tada: Happy work anniversary! :clap:\n*Example Co*\n• Alice Example - 3 years",
        )

    def test_anniversary_message_names_the_company_once_per_group(self):
        """People are bulleted under their company name, one group per company."""
        alice = _employee("Alice Example", date_of_joining=date(2023, 6, 15))
        bob = _employee("Bob Example", date_of_joining=date(2021, 6, 15))
        carol = _employee("Carol Example", company="Other Co", date_of_joining=date(2025, 6, 15))
        mock_slack, _ = self._run(_build_settings_mock(), anniversaries=[alice, bob, carol])
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args),
            ":tada: Happy work anniversary! :clap:\n"
            "*Example Co*\n• Alice Example - 3 years\n• Bob Example - 5 years\n"
            "*Other Co*\n• Carol Example - 1 year",
        )

    def test_anniversary_message_uses_singular_year(self):
        """One completed year is written as '1 year'."""
        alice = _employee("Alice Example", date_of_joining=date(2025, 6, 15))
        mock_slack, _ = self._run(_build_settings_mock(), anniversaries=[alice])
        self.assertIn(
            "• Alice Example - 1 year", self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args)
        )

    def test_posts_birthdays_and_anniversaries_as_separate_messages(self):
        """Birthdays and anniversaries on the same day are two posts, birthdays first."""
        alice = _employee("Alice Example")
        bob = _employee("Bob Example")
        mock_slack, _ = self._run(_build_settings_mock(), birthdays=[alice], anniversaries=[bob])
        calls = mock_slack.slack_app.client.chat_postMessage.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["blocks"][0]["text"]["text"], ":birthday: Birthdays")
        self.assertEqual(calls[1].kwargs["blocks"][0]["text"]["text"], ":tada: Work Anniversaries")

    def test_renders_linked_email_template(self):
        """The Response (HTML) of the Email Template linked in Slack Settings replaces the default message text."""
        alice = _employee("Alice Example")
        settings = _build_settings_mock(birthday_message_template="FSC Birthday")
        mock_slack, mock_log = self._run(
            settings,
            birthdays=[alice],
            templates={"FSC Birthday": "Cake for {% for e in employees %}{{ e.name }}{% endfor %} today"},
        )
        mock_log.assert_not_called()
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args), "Cake for Alice Example today"
        )

    def test_uses_default_and_logs_when_linked_template_is_missing(self):
        """A link to an Email Template that no longer exists is logged and the built-in message is posted anyway."""
        settings = _build_settings_mock(birthday_message_template="FSC Gone")
        mock_slack, mock_log = self._run(settings, birthdays=[_employee("Alice")], templates={})
        mock_log.assert_called_once()
        self.assertIn("FSC Gone", mock_log.call_args.kwargs["message"])
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args),
            ":birthday: Happy birthday! :tada:\n• Alice",
        )

    def test_uses_default_and_logs_when_linked_template_has_use_html_off(self):
        """An Email Template whose message is in the rich-text Response (Use HTML off) is not rendered: it is logged and the default is posted."""
        settings = _build_settings_mock(anniversary_message_template="FSC Anniv")
        mock_slack, mock_log = self._run(
            settings,
            anniversaries=[_employee("Alice")],
            templates={"FSC Anniv": {"use_html": 0, "response_html": None}},
        )
        mock_log.assert_called_once()
        self.assertIn(
            "Happy work anniversary! :clap:\n*Example Co*\n• Alice",
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args),
        )

    def test_uses_default_and_logs_when_linked_template_is_blank(self):
        """An Email Template with Use HTML on but an empty Response (HTML) falls back to the default with a log."""
        settings = _build_settings_mock(birthday_message_template="FSC Blank")
        mock_slack, mock_log = self._run(
            settings, birthdays=[_employee("Alice")], templates={"FSC Blank": {"use_html": 1, "response_html": "  "}}
        )
        mock_log.assert_called_once()
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args),
            ":birthday: Happy birthday! :tada:\n• Alice",
        )

    def test_does_not_look_up_email_template_when_nothing_is_linked(self):
        """With no template linked, no Email Template query is made (the _run lookup fails the test if called) and the default is used."""
        mock_slack, mock_log = self._run(_build_settings_mock(), birthdays=[_employee("Alice")])
        mock_log.assert_not_called()
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args),
            ":birthday: Happy birthday! :tada:\n• Alice",
        )

    def test_falls_back_to_attendance_channel(self):
        """With no celebrations channel configured the post goes to the attendance channel."""
        mock_slack, _ = self._run(_build_settings_mock(celebrations_channel_id=None), birthdays=[_employee("Alice")])
        self.assertEqual(
            mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["channel"], TEST_SLACK_CHANNEL_ID
        )

    def test_logs_error_and_skips_post_when_no_channel_configured(self):
        """With neither a celebrations nor an attendance channel the post is skipped and an error is logged."""
        events = {"birthday": _grouped(_employee("Alice")), "work_anniversary": {}}
        mock_slack = MagicMock()
        mock_slack.SLACK_CHANNEL_ID = None
        with (
            patch(
                f"{CELEBRATIONS_MODULE}.frappe.get_single",
                return_value=_build_settings_mock(celebrations_channel_id=""),
            ),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=TODAY),
            patch(f"{CELEBRATIONS_MODULE}.get_employees_having_an_event_today", side_effect=events.get),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log,
        ):
            send_celebrations()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_logs_error_and_still_posts_anniversaries_when_birthday_post_fails(self):
        """A failing birthday post is logged and does not stop the anniversary post."""
        alice = _employee("Alice Example")
        bob = _employee("Bob Example")
        events = {"birthday": _grouped(alice), "work_anniversary": _grouped(bob)}
        mock_slack = MagicMock()
        mock_slack.slack_app.client.chat_postMessage.side_effect = [RuntimeError("channel_not_found"), {"ok": True}]
        with (
            patch(f"{CELEBRATIONS_MODULE}.frappe.get_single", return_value=_build_settings_mock()),
            patch(f"{CELEBRATIONS_MODULE}.frappe.utils.nowdate", return_value=TODAY),
            patch(f"{CELEBRATIONS_MODULE}.get_employees_having_an_event_today", side_effect=events.get),
            patch(f"{CELEBRATIONS_MODULE}.get_slack_user_ids", return_value={}),
            patch(f"{CELEBRATIONS_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{CELEBRATIONS_MODULE}.generate_error_log") as mock_log,
        ):
            send_celebrations()
        mock_log.assert_called_once()
        calls = mock_slack.slack_app.client.chat_postMessage.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["blocks"][0]["text"]["text"], ":tada: Work Anniversaries")

    def test_logs_error_when_template_fails_to_render(self):
        """A template that errors at render time is logged and nothing is posted for that event type."""
        settings = _build_settings_mock(birthday_message_template="FSC Bad")
        mock_slack, mock_log = self._run(
            settings, birthdays=[_employee("Alice")], templates={"FSC Bad": "{{ employees.oops.deeper }}"}
        )
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_birthday_context_has_no_years(self):
        """The birthday template context carries name, mention and company only; years is anniversary-only."""
        template = "{% for e in employees %}{{ e.keys() | sort | join(',') }}{% endfor %}"
        settings = _build_settings_mock(birthday_message_template="FSC Keys", anniversary_message_template="FSC Keys")
        mock_slack, _ = self._run(
            settings,
            birthdays=[_employee("Alice")],
            anniversaries=[_employee("Bob")],
            templates={"FSC Keys": template},
        )
        calls = mock_slack.slack_app.client.chat_postMessage.call_args_list
        self.assertEqual(self._section_text(calls[0]), "company,mention,name")
        self.assertEqual(self._section_text(calls[1]), "company,mention,name,years")

    def test_fallback_text_uses_escaped_names(self):
        """The plain-text notification fallback is built from escaped names so a name cannot inject a Slack mention."""
        mock_slack, _ = self._run(_build_settings_mock(), birthdays=[_employee("Tom <!channel>")])
        text = mock_slack.slack_app.client.chat_postMessage.call_args.kwargs["text"]
        self.assertIn("Tom &lt;!channel&gt;", text)
        self.assertNotIn("<!channel>", text)

    def test_logs_error_when_fallback_text_fails(self):
        """A failure while building the fallback text is logged for that event type and nothing is posted for it."""
        with patch(f"{CELEBRATIONS_MODULE}.get_work_anniversary_reminder_text", side_effect=RuntimeError("boom")):
            mock_slack, mock_log = self._run(_build_settings_mock(), anniversaries=[_employee("Alice")])
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()
        mock_log.assert_called_once()

    def test_renders_template_as_string_not_path(self):
        """A one-line template ending in a file extension is rendered as text, not looked up as a template file."""
        settings = _build_settings_mock(birthday_message_template="FSC Notes")
        mock_slack, mock_log = self._run(
            settings,
            birthdays=[_employee("Alice")],
            templates={"FSC Notes": "See {{ employees[0].name }} in notes.txt"},
        )
        mock_log.assert_not_called()
        self.assertEqual(
            self._section_text(mock_slack.slack_app.client.chat_postMessage.call_args), "See Alice in notes.txt"
        )


class TestGetEmployeesWithEvent(IntegrationTestCase):
    def test_flattens_company_groups(self):
        """get_employees_with_event returns HRMS's per-company groups as one flat list."""
        alice = _employee("Alice", company="A")
        bob = _employee("Bob", company="B")
        with patch(
            f"{CELEBRATIONS_MODULE}.get_employees_having_an_event_today", return_value=_grouped(alice, bob)
        ) as mock_query:
            result = get_employees_with_event("birthday")
        mock_query.assert_called_once_with("birthday")
        self.assertEqual(result, [alice, bob])

    def test_returns_empty_list_when_query_returns_nothing(self):
        """get_employees_with_event returns [] for an empty or None HRMS result."""
        with patch(f"{CELEBRATIONS_MODULE}.get_employees_having_an_event_today", return_value=None):
            self.assertEqual(get_employees_with_event("work_anniversary"), [])


class TestGetSlackUserIds(IntegrationTestCase):
    def test_returns_empty_dict_for_no_users(self):
        """get_slack_user_ids returns {} without querying when given no user ids."""
        with patch(f"{CELEBRATIONS_MODULE}.frappe.get_all") as mock_get_all:
            self.assertEqual(get_slack_user_ids([]), {})
        mock_get_all.assert_not_called()

    def test_maps_users_to_slack_ids_from_user_meta(self):
        """get_slack_user_ids maps a Frappe user to the Slack ID stored in User Meta and skips unknown users."""
        make_test_user(TEST_USER)
        make_test_user_meta(TEST_USER, slack_userid=TEST_SLACK_USER_ID)
        result = get_slack_user_ids([TEST_USER, "nobody@example.com"])
        self.assertEqual(result, {TEST_USER: TEST_SLACK_USER_ID})

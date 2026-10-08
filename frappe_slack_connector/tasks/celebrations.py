import calendar
import datetime

import frappe
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from frappe import _
from frappe.utils import add_days, get_time, getdate

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration

OPT_OUT_FIELD = "custom_skip_celebration_announcements"

# Longest run of consecutive non-working days we roll back over when looking
# for events that fell on a weekend or holiday.
MAX_ROLLBACK_DAYS = 31

DEFAULT_BIRTHDAY_TEMPLATE = (
    ":birthday: Happy birthday "
    "{% for e in employees %}{{ e.mention }}"
    "{% if not e.is_today %} (for {{ e.day_name }}){% endif %}"
    "{% if not loop.last %}, {% endif %}{% endfor %}"
    "! :tada:"
)

DEFAULT_ANNIVERSARY_TEMPLATE = (
    ":tada: Happy work anniversary "
    "{% for e in employees %}{{ e.mention }} - {{ e.years }} year{{ '' if e.years == 1 else 's' }} at {{ e.company }}"
    "{% if not e.is_today %} (on {{ e.day_name }}){% endif %}"
    "{% if not loop.last %}, {% endif %}{% endfor %}"
    "! :clap:"
)


def celebrations_channel() -> None:
    """
    Scheduler entry (runs every minute) that posts the daily birthday and
    work-anniversary announcements to Slack.
    Conditions:
     - At least one of the two event types is enabled
     - Today is a working day (events on weekends and holidays are picked
       up by send_celebrations on the next working day)
     - Today's announcements have not already been queued
     - The configured celebrations time has passed
    The actual work is enqueued so the scheduler tick stays short.
    """
    slack_settings = frappe.get_single("Slack Settings")
    if not (slack_settings.send_birthday_updates or slack_settings.send_anniversary_updates):
        return

    current_date = frappe.utils.nowdate()
    if (
        not is_working_day(getdate(current_date), get_default_holiday_list())
        or (
            slack_settings.last_celebrations_date is not None
            and getdate(slack_settings.last_celebrations_date) == getdate(current_date)
        )
        or frappe.utils.now_datetime().time() < get_time(slack_settings.celebrations_time or "09:00:00")
    ):
        return

    frappe.enqueue(send_celebrations, queue="short", date=current_date)

    # Stamp the date as soon as the job is queued so the next scheduler tick
    # does not queue it again, even when nobody has an event today.
    slack_settings.last_celebrations_date = current_date
    slack_settings.save(ignore_permissions=True)


def send_celebrations(date: str | datetime.date) -> None:
    """
    Background job: post one birthday message and one work-anniversary
    message for every event that falls on `date` or on the run of
    non-working days immediately before it. Nothing is posted for an event
    type with no employees.
    """
    run_date = getdate(date)
    slack_settings = frappe.get_single("Slack Settings")
    slack = SlackIntegration()
    channel = slack_settings.celebrations_channel_id or slack.SLACK_CHANNEL_ID

    start_date, end_date = get_celebration_window(run_date, get_default_holiday_list())
    employees = get_active_employees()

    birthdays = (
        get_employees_with_birthday(end_date, start_date=start_date, employees=employees)
        if slack_settings.send_birthday_updates
        else []
    )
    anniversaries = (
        get_employees_with_anniversary(end_date, start_date=start_date, employees=employees)
        if slack_settings.send_anniversary_updates
        else []
    )
    if not birthdays and not anniversaries:
        return

    user_ids = {e.user_id for e in [*birthdays, *anniversaries] if e.user_id}
    slack_user_ids = get_slack_user_ids(list(user_ids))
    mention_users = bool(slack_settings.mention_user)

    def to_context(employee) -> dict:
        slack_userid = slack_user_ids.get(employee.user_id) if employee.user_id else None
        context = {
            "name": employee.employee_name,
            "mention": f"<@{slack_userid}>" if slack_userid and mention_users else employee.employee_name,
            "company": employee.company,
            "date": employee.event_date,
        }
        if "years" in employee:
            context["years"] = employee.years
        return context

    if birthdays:
        post_blocks(
            slack,
            channel,
            build_birthday_blocks(
                [to_context(e) for e in birthdays],
                run_date,
                template=slack_settings.birthday_message_template,
            ),
            error_title=_("Error posting birthday announcement to Slack"),
        )

    if anniversaries:
        post_blocks(
            slack,
            channel,
            build_anniversary_blocks(
                [to_context(e) for e in anniversaries],
                run_date,
                template=slack_settings.anniversary_message_template,
            ),
            error_title=_("Error posting work anniversary announcement to Slack"),
        )


def post_blocks(slack: SlackIntegration, channel: str, blocks: list, *, error_title: str) -> None:
    """
    Post the blocks to the channel, logging (not raising) on failure so a
    failed birthday post does not stop the anniversary post.
    """
    try:
        slack.slack_app.client.chat_postMessage(channel=channel, blocks=blocks)
    except Exception as e:
        generate_error_log(
            title=error_title,
            message=_("Please check the celebrations channel ID and try again."),
            exception=e,
        )


def get_default_holiday_list() -> str | None:
    """
    Holiday list of the default company, used to decide working days
    """
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        return None
    return frappe.db.get_value("Company", company, "default_holiday_list")


def is_working_day(day: datetime.date, holiday_list: str | None) -> bool:
    """
    A day is a working day when it is Monday to Friday and not a holiday
    in the given holiday list
    """
    if day.weekday() > 4:  # sat = 5, sun = 6
        return False
    return not (holiday_list and is_holiday(holiday_list, day))


def get_celebration_window(run_date: datetime.date, holiday_list: str | None) -> tuple[datetime.date, datetime.date]:
    """
    Return the (start, end) date range whose events are announced on
    `run_date`: `run_date` itself plus the run of non-working days
    immediately before it, so weekend and holiday events are not lost.
    """
    start = run_date
    previous = add_days(run_date, -1)
    for _i in range(MAX_ROLLBACK_DAYS):
        if is_working_day(previous, holiday_list):
            break
        start = previous
        previous = add_days(previous, -1)
    return start, run_date


def get_active_employees() -> list:
    """
    Active employees with the fields needed for celebrations
    """
    fields = ["name", "employee_name", "user_id", "company", "date_of_birth", "date_of_joining"]
    if frappe.get_meta("Employee").has_field(OPT_OUT_FIELD):
        fields.append(OPT_OUT_FIELD)
    return frappe.get_all("Employee", filters={"status": "Active"}, fields=fields)


def get_slack_user_ids(user_ids: list[str]) -> dict[str, str]:
    """
    Map Frappe user ids to Slack user ids from User Meta, in one query
    """
    if not user_ids:
        return {}
    user_metas = frappe.get_all(
        "User Meta",
        filters={"user": ["in", user_ids]},
        fields=["user", "custom_slack_userid"],
    )
    return {um.user: um.custom_slack_userid for um in user_metas if um.custom_slack_userid}


def observed_date(event: datetime.date, year: int) -> datetime.date:
    """
    The calendar day on which an annual event is observed in `year`.
    A 29 Feb event is observed on 28 Feb when `year` has no 29 Feb.
    """
    if event.month == 2 and event.day == 29 and not calendar.isleap(year):
        return datetime.date(year, 2, 28)
    return datetime.date(year, event.month, event.day)


def _events_in_window(employees: list, field: str, start_date: datetime.date, end_date: datetime.date) -> list:
    """
    Employees whose annual event stored in `field` is observed between
    start_date and end_date (inclusive). Each returned row is a copy of the
    employee row with `event_date` set to the observed date in that window.
    Opted-out employees and rows without the field are skipped.
    """
    matches = []
    for employee in employees:
        if employee.get(OPT_OUT_FIELD) or not employee.get(field):
            continue
        event = getdate(employee.get(field))
        for year in range(start_date.year, end_date.year + 1):
            observed = observed_date(event, year)
            if start_date <= observed <= end_date:
                row = frappe._dict(employee)
                row.event_date = observed
                matches.append(row)
                break
    return matches


def get_employees_with_birthday(
    date: datetime.date, *, start_date: datetime.date | None = None, employees: list | None = None
) -> list:
    """
    Active, not opted-out employees whose birthday falls between start_date
    (default: `date`) and `date`. The birth year is never exposed.
    """
    run_date = getdate(date)
    start = getdate(start_date) if start_date else run_date
    if employees is None:
        employees = get_active_employees()
    return _events_in_window(employees, "date_of_birth", start, run_date)


def get_employees_with_anniversary(
    date: datetime.date, *, start_date: datetime.date | None = None, employees: list | None = None
) -> list:
    """
    Active, not opted-out employees whose work anniversary falls between
    start_date (default: `date`) and `date`, with `years` set to the number
    of completed years. Employees who joined in the same year are excluded.
    """
    run_date = getdate(date)
    start = getdate(start_date) if start_date else run_date
    if employees is None:
        employees = get_active_employees()
    matches = []
    for row in _events_in_window(employees, "date_of_joining", start, run_date):
        joined = getdate(row.date_of_joining)
        if joined.year >= row.event_date.year:
            continue
        row.years = row.event_date.year - joined.year
        matches.append(row)
    return matches


def _with_day_info(employees: list, run_date: datetime.date) -> list:
    """
    Add `is_today` and `day_name` to each employee context, derived from
    the employee's event `date`, so templates can say "for Saturday"
    """
    prepared = []
    for employee in employees:
        context = dict(employee)
        event_date = getdate(context["date"]) if context.get("date") else run_date
        context.setdefault("is_today", event_date == run_date)
        context.setdefault("day_name", event_date.strftime("%A"))
        prepared.append(context)
    return prepared


def _render(template: str | None, default: str, employees: list, run_date: datetime.date) -> str:
    context = {"employees": _with_day_info(employees, run_date), "date": run_date}
    # Templates come from Slack Settings (System Manager only) or the module defaults
    # nosemgrep
    return frappe.render_template(template or default, context).strip()


def _blocks(header: str, text: str) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
    ]


def build_birthday_blocks(employees: list, date: datetime.date, template: str | None = None) -> list:
    """
    Slack blocks for the birthday announcement: a header plus one section
    rendered from the Jinja template (or the default when empty)
    """
    return _blocks(":birthday: Birthdays", _render(template, DEFAULT_BIRTHDAY_TEMPLATE, employees, date))


def build_anniversary_blocks(employees: list, date: datetime.date, template: str | None = None) -> list:
    """
    Slack blocks for the work-anniversary announcement: a header plus one
    section rendered from the Jinja template (or the default when empty)
    """
    return _blocks(":tada: Work Anniversaries", _render(template, DEFAULT_ANNIVERSARY_TEMPLATE, employees, date))

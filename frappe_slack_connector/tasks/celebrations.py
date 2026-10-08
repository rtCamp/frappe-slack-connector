import calendar
import datetime
from collections.abc import Callable

import frappe
from erpnext.setup.doctype.holiday_list.holiday_list import is_holiday
from frappe import _
from frappe.utils import add_days, get_time, getdate

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.str_utils import escape_slack_text
from frappe_slack_connector.slack.app import SlackIntegration

OPT_OUT_FIELD = "custom_skip_celebration_announcements"

# Longest run of days we look back over, both for consecutive non-working
# days and for catching up after days on which the job did not run. Kept
# short so (re-)enabling the feature after a gap cannot flood the channel.
MAX_ROLLBACK_DAYS = 7

# Events further back than this are described by date instead of weekday
# name in the template context, since "for Tuesday" would be ambiguous.
WEEKDAY_NAME_MAX_AGE_DAYS = 6

CLAIM_KEY_PREFIX = "fsc_celebrations_posted"
CLAIM_TTL_SECONDS = 2 * 86400

# Slack rejects section blocks whose text is longer than this.
SLACK_SECTION_TEXT_LIMIT = 3000

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
    Scheduler entry (runs every scheduler tick, i.e. every
    `scheduler_interval`, 240s by default) that queues the daily birthday
    and work-anniversary announcements.
    Conditions, cheapest first:
     - At least one of the two event types is enabled
     - Today's announcements have not already been queued
     - The configured celebrations time has passed
     - Today is a working day (events on weekends and holidays are picked
       up by send_celebrations on the next working day)
    The date is stamped before the job is queued so a second tick cannot
    queue it again; the job itself is deduplicated by id as well.
    """
    slack_settings = frappe.get_single("Slack Settings")
    if not (slack_settings.send_birthday_updates or slack_settings.send_anniversary_updates):
        return

    current_date = frappe.utils.nowdate()
    previous_run = slack_settings.last_celebrations_date
    if (
        (previous_run is not None and getdate(previous_run) == getdate(current_date))
        or frappe.utils.now_datetime().time() < get_time(slack_settings.celebrations_time or "09:00:00")
        or not is_working_day(getdate(current_date), get_default_holiday_list())
    ):
        return

    # Stamp without touching `modified` so this cannot collide with another
    # full save of Slack Settings (e.g. the attendance summary) on the same tick.
    frappe.db.set_single_value("Slack Settings", "last_celebrations_date", current_date, update_modified=False)
    frappe.enqueue(
        send_celebrations,
        queue="short",
        enqueue_after_commit=True,
        job_id=f"celebrations::{current_date}",
        deduplicate=True,
        date=current_date,
        previous_run=str(previous_run) if previous_run else None,
    )


def send_celebrations(date: str | datetime.date, previous_run: str | datetime.date | None = None) -> None:
    """
    Background job: post one birthday message and one work-anniversary
    message for every event that falls on `date`, on the run of non-working
    days immediately before it, or on any day since `previous_run` that was
    missed. Nothing is posted for an event type with no employees.
    """
    run_date = getdate(date)
    if not claim_celebrations_day(run_date):
        frappe.logger().info(f"Celebrations for {run_date} already posted, skipping duplicate job")
        return

    slack_settings = frappe.get_single("Slack Settings")

    start_date, end_date = get_celebration_window(
        run_date,
        get_default_holiday_list(),
        previous_run=getdate(previous_run) if previous_run else None,
    )
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

    slack = SlackIntegration()
    channel = slack_settings.celebrations_channel_id or slack.SLACK_CHANNEL_ID
    if not channel:
        generate_error_log(
            title=_("Celebrations channel not set"),
            message=_("Set the Celebrations Channel ID or the Attendance Channel ID in Slack Settings."),
        )
        return

    user_ids = {e.user_id for e in [*birthdays, *anniversaries] if e.user_id}
    slack_user_ids = get_slack_user_ids(list(user_ids))
    mention_users = bool(slack_settings.mention_user)

    def to_context(employee) -> dict:
        slack_userid = slack_user_ids.get(employee.user_id) if employee.user_id else None
        name = escape_slack_text(employee.employee_name)
        context = {
            "name": name,
            "mention": f"<@{slack_userid}>" if slack_userid and mention_users else name,
            "company": escape_slack_text(employee.company),
            "date": employee.event_date,
        }
        if "years" in employee:
            context["years"] = employee.years
        return context

    if birthdays:
        post_announcement(
            slack,
            channel,
            build_birthday_blocks,
            [to_context(e) for e in birthdays],
            run_date,
            template=slack_settings.birthday_message_template,
            fallback_prefix=_("Birthdays"),
            error_title=_("Error posting birthday announcement to Slack"),
        )

    if anniversaries:
        post_announcement(
            slack,
            channel,
            build_anniversary_blocks,
            [to_context(e) for e in anniversaries],
            run_date,
            template=slack_settings.anniversary_message_template,
            fallback_prefix=_("Work anniversaries"),
            error_title=_("Error posting work anniversary announcement to Slack"),
        )


def post_announcement(
    slack: SlackIntegration,
    channel: str,
    build_blocks: Callable,
    employees: list,
    run_date: datetime.date,
    *,
    template: str | None,
    fallback_prefix: str,
    error_title: str,
) -> None:
    """
    Render and post one announcement, logging (not raising) on failure so a
    broken birthday template or post does not stop the anniversary post.
    Nothing is posted when the template renders to an empty message.
    """
    try:
        blocks = build_blocks(employees, run_date, template=template)
        if not blocks:
            return
        slack.slack_app.client.chat_postMessage(
            channel=channel,
            blocks=blocks,
            text=f"{fallback_prefix}: {', '.join(e['name'] for e in employees)}",
        )
    except Exception as e:
        generate_error_log(
            title=error_title,
            message=_("Please check the celebrations channel ID and message template and try again."),
            exception=e,
        )


def claim_celebrations_day(run_date: datetime.date) -> bool:
    """
    Atomically mark `run_date` as posted in Redis (SET NX with a two-day
    TTL). Returns False when another job already claimed it, which makes
    the post idempotent even if last_celebrations_date is overwritten by a
    stale save of Slack Settings and the scheduler queues the day again.
    """
    # Raw redis SET NX (set_value has no atomic "only if absent"); the key is
    # site-prefixed via make_key, so multitenancy is preserved.
    key = frappe.cache.make_key(f"{CLAIM_KEY_PREFIX}::{run_date}")
    # nosemgrep
    return bool(frappe.cache.set(key, 1, nx=True, ex=CLAIM_TTL_SECONDS))


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


def get_celebration_window(
    run_date: datetime.date, holiday_list: str | None, previous_run: datetime.date | None = None
) -> tuple[datetime.date, datetime.date]:
    """
    Return the (start, end) date range whose events are announced on
    `run_date`: `run_date` itself plus the run of non-working days
    immediately before it, so weekend and holiday events are not lost.
    When `previous_run` (the last date the job ran) is given, the window
    also covers every day since then, so days on which the job did not run
    are caught up, looking back at most MAX_ROLLBACK_DAYS.
    """
    start = run_date
    previous = add_days(run_date, -1)
    for _i in range(MAX_ROLLBACK_DAYS):
        if is_working_day(previous, holiday_list):
            break
        start = previous
        previous = add_days(previous, -1)
    if previous_run is not None:
        catch_up_start = max(add_days(previous_run, 1), add_days(run_date, -MAX_ROLLBACK_DAYS))
        start = min(start, catch_up_start)
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


def describe_event_day(event_date: datetime.date, run_date: datetime.date) -> str:
    """
    Weekday name for recent events ("Saturday"); a short date ("Oct 1")
    for events older than WEEKDAY_NAME_MAX_AGE_DAYS, where a weekday name
    would be ambiguous.
    """
    if (run_date - event_date).days > WEEKDAY_NAME_MAX_AGE_DAYS:
        return f"{event_date.strftime('%b')} {event_date.day}"
    return event_date.strftime("%A")


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
        context.setdefault("day_name", describe_event_day(event_date, run_date))
        prepared.append(context)
    return prepared


def _render(template: str | None, default: str, employees: list, run_date: datetime.date) -> str:
    context = {"employees": _with_day_info(employees, run_date), "date": run_date}
    # Templates come from Slack Settings (System Manager only) or the module defaults
    # nosemgrep
    return frappe.render_template(template or default, context).strip()


def _join_within(parts: list[str], separator: str, limit: int) -> list[str]:
    """
    Greedily join `parts` with `separator` into strings of at most `limit`
    characters. A single part longer than `limit` is passed through as is.
    """
    chunks = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current}{separator}{part}"
        if len(candidate) > limit and current:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_text(text: str, limit: int = SLACK_SECTION_TEXT_LIMIT) -> list[str]:
    """
    Split text into chunks of at most `limit` characters, preferring line
    boundaries, then ", " separators (so a mention or entity is not cut in
    the middle); only a single comma-free run longer than `limit` is cut hard.
    """
    pieces = []
    for line in text.split("\n"):
        if len(line) <= limit:
            pieces.append(line)
            continue
        for segment in _join_within(line.split(", "), ", ", limit):
            pieces.extend(segment[i : i + limit] for i in range(0, len(segment), limit))
    return _join_within(pieces, "\n", limit)


def _blocks(header: str, text: str) -> list:
    """
    A header block followed by one section block per chunk of `text`.
    Returns an empty list when there is no text to post.
    """
    if not text.strip():
        return []
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        },
        *(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            }
            for chunk in split_text(text)
        ),
    ]


def build_birthday_blocks(employees: list, date: datetime.date, template: str | None = None) -> list:
    """
    Slack blocks for the birthday announcement: a header plus the section(s)
    rendered from the Jinja template (or the default when empty)
    """
    return _blocks(":birthday: Birthdays", _render(template, DEFAULT_BIRTHDAY_TEMPLATE, employees, date))


def build_anniversary_blocks(employees: list, date: datetime.date, template: str | None = None) -> list:
    """
    Slack blocks for the work-anniversary announcement: a header plus the
    section(s) rendered from the Jinja template (or the default when empty)
    """
    return _blocks(":tada: Work Anniversaries", _render(template, DEFAULT_ANNIVERSARY_TEMPLATE, employees, date))

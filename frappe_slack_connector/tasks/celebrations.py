from collections.abc import Callable

import frappe
from frappe import _
from frappe.utils import get_time, getdate
from hrms.controllers.employee_reminders import (
    get_birthday_reminder_text_and_message,
    get_employees_having_an_event_today,
    get_work_anniversary_reminder_text,
)

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.str_utils import escape_slack_text
from frappe_slack_connector.slack.app import SlackIntegration

DEFAULT_BIRTHDAY_TEMPLATE = (
    ":birthday: Happy birthday "
    "{% for e in employees %}{{ e.mention }}{% if not loop.last %}, {% endif %}{% endfor %}"
    "! :tada:"
)

DEFAULT_ANNIVERSARY_TEMPLATE = (
    ":tada: Happy work anniversary "
    "{% for e in employees %}{{ e.mention }} - {{ e.years }} year{{ '' if e.years == 1 else 's' }} at {{ e.company }}"
    "{% if not loop.last %}, {% endif %}{% endfor %}"
    "! :clap:"
)


def celebrations_channel() -> None:
    """
    Scheduler entry (every tick) that posts today's birthday and work
    anniversary announcements to the Slack channel
    Conditions:
     - Celebration updates are enabled
     - Today's announcements have not been posted yet
     - The configured celebrations time has passed
    Then posts the announcements and stamps the date in Slack Settings
    """
    slack_settings = frappe.get_single("Slack Settings")
    if slack_settings.send_celebration_updates != 1:
        return

    current_date = frappe.utils.nowdate()
    if (
        slack_settings.last_celebrations_date is not None
        and getdate(slack_settings.last_celebrations_date) == getdate(current_date)
    ) or frappe.utils.now_datetime().time() < get_time(slack_settings.celebrations_time or "09:00:00"):
        return

    send_celebrations()

    # Stamp the column directly: a full save would run validate and the
    # modified check, and a bad template or a concurrent save of Slack
    # Settings would lose the stamp and repost on every tick.
    frappe.db.set_single_value("Slack Settings", "last_celebrations_date", current_date)


def send_celebrations() -> None:
    """
    Post one birthday message and one work anniversary message for the
    active employees whose event is today, each grouping everyone who
    shares the day. Nothing is posted for an event type with nobody.
    """
    slack_settings = frappe.get_single("Slack Settings")
    birthdays = get_employees_with_event("birthday")
    anniversaries = get_employees_with_event("work_anniversary")
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

    slack_user_ids = get_slack_user_ids([e.user_id for e in [*birthdays, *anniversaries] if e.user_id])
    current_year = getdate(frappe.utils.nowdate()).year

    def to_context(employee, *, with_years: bool) -> dict:
        # Always mention whoever has a Slack ID (the "Mention User" setting is
        # for the daily attendance summary): a birthday or work anniversary
        # comes once a year and the person should see it
        slack_userid = slack_user_ids.get(employee.user_id) if employee.user_id else None
        name = escape_slack_text(employee.name)
        context = {
            "name": name,
            "mention": f"<@{slack_userid}>" if slack_userid else name,
            "company": escape_slack_text(employee.company),
        }
        if with_years:
            context["years"] = current_year - getdate(employee.date_of_joining).year
        return context

    if birthdays:
        post_announcement(
            slack,
            channel,
            header=":birthday: Birthdays",
            template=get_celebration_template(slack_settings.birthday_message_template, DEFAULT_BIRTHDAY_TEMPLATE),
            employees=birthdays,
            to_context=lambda e: to_context(e, with_years=False),
            fallback_text=lambda rows: get_birthday_reminder_text_and_message(rows)[0],
            error_title=_("Error posting birthday announcement to Slack"),
        )

    if anniversaries:
        post_announcement(
            slack,
            channel,
            header=":tada: Work Anniversaries",
            template=get_celebration_template(
                slack_settings.anniversary_message_template, DEFAULT_ANNIVERSARY_TEMPLATE
            ),
            employees=anniversaries,
            to_context=lambda e: to_context(e, with_years=True),
            fallback_text=get_work_anniversary_reminder_text,
            error_title=_("Error posting work anniversary announcement to Slack"),
        )


def get_celebration_template(template_name: str | None, default: str) -> str:
    """
    The Jinja source for an announcement: the Response (HTML) of the Email
    Template linked from Slack Settings, the same field the timesheet
    reminder renders, or the built-in default when nothing is linked.
    A linked template that no longer exists or does not use HTML (its
    rich-text response would post raw HTML) is logged and the default is
    used, so the announcement still goes out.
    """
    if not template_name:
        return default
    template = frappe.db.get_value("Email Template", template_name, ["use_html", "response_html"], as_dict=True)
    if template and template.use_html and (template.response_html or "").strip():
        return template.response_html
    generate_error_log(
        title=_("Celebrations template not usable, using the default message"),
        message=_(
            "Email Template {0} is missing, has Use HTML off or has an empty Response (HTML). "
            "Fix it or clear the link in Slack Settings."
        ).format(template_name),
    )
    return default


def post_announcement(
    slack: SlackIntegration,
    channel: str,
    *,
    header: str,
    template: str,
    employees: list,
    to_context: Callable[[dict], dict],
    fallback_text: Callable[[list], str],
    error_title: str,
) -> None:
    """
    Render the Jinja template with the employees' template context and
    post it under a header block, with the HRMS reminder text (built from
    escaped names) as the notification fallback.
    Failures are logged, not raised, so a broken birthday template or post
    does not stop the anniversary post.
    """
    try:
        escaped = [frappe._dict(e, name=escape_slack_text(e.name)) for e in employees]
        text = render_slack_template(template, {"employees": [to_context(e) for e in employees]})
        slack.slack_app.client.chat_postMessage(
            channel=channel,
            text=fallback_text(escaped),
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": header, "emoji": True},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text.strip()},
                },
            ],
        )
    except Exception as e:
        generate_error_log(
            title=error_title,
            message=_("Please check the celebrations channel ID and message template and try again."),
            exception=e,
        )


def render_slack_template(template: str, context: dict) -> str:
    """
    Render a message template from Slack Settings as a string.
    frappe.render_template treats a one-line template ending in .txt or
    .html as a file path, so use the sandboxed environment directly,
    mirroring its string branch. Jinja errors propagate to the caller.
    """
    from frappe.utils.jinja import get_jenv, safe_render_flags

    if ".__" in template:
        frappe.throw(_("Illegal template"))
    with safe_render_flags():
        return get_jenv().from_string(template).render(context)


def get_employees_with_event(event_type: str) -> list:
    """
    Active employees whose `event_type` ("birthday" or "work_anniversary")
    is today, in one flat list (HRMS groups them by company). Employees
    whose event date is in the current year, i.e. who joined today, are
    excluded by the HRMS query.
    """
    grouped = get_employees_having_an_event_today(event_type) or {}
    return [employee for employees in grouped.values() for employee in employees]


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

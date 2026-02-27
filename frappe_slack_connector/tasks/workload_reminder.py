import frappe
from frappe import _
from frappe.utils import add_days, get_weekday, getdate

from frappe_slack_connector.db.employee import check_if_date_is_holiday
from frappe_slack_connector.db.timesheet import is_next_pms_installed
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration

IMPORT_SUCCESS = True

try:
    from next_pms.resource_management.api.utils.query import get_allocation_list_for_employee_for_given_range
except ImportError:
    IMPORT_SUCCESS = False

    def get_allocation_list_for_employee_for_given_range(*args, **kwargs):
        frappe.throw(
            _(
                "get_allocation_list_for_employee_for_given_range is not implemented because next_pms module is missing."
            ),
            exc=NotImplementedError,
        )


STANDARD_HOURS = 8


def get_workload_data(start_date, end_date):
    """Fetch employees, their allocations, and leaves in the given date range."""

    # Fetch target designations from Timesheet Settings
    timesheet_settings = frappe.get_doc("Timesheet Settings")
    designations = [d.designation for d in timesheet_settings.designations]

    if not designations:
        return [], {}, {}

    # Fetch Active employees matching the specified designations
    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active", "designation": ["in", designations]},
        fields=["name", "employee_name", "reports_to", "user_id"],
    )

    employee_names = [emp.name for emp in employees]
    if not employee_names:
        return [], {}, {}

    allocations = get_allocation_list_for_employee_for_given_range(
        columns=[
            "employee",
            "allocation_start_date",
            "allocation_end_date",
            "hours_allocated_per_day",
        ],
        value_key="employee",
        values=employee_names,
        start_date=start_date,
        end_date=end_date,
    )

    leaves = frappe.get_all(
        "Leave Application",
        filters=[
            ["employee", "in", employee_names],
            ["docstatus", "in", [0, 1]],
            ["status", "in", ["Open", "Approved"]],
            ["from_date", "<=", end_date],
            ["to_date", ">=", start_date],
        ],
        fields=["employee", "from_date", "to_date"],
    )

    # Group allocations and leaves by employee
    allocation_map = {}
    for alloc in allocations:
        allocation_map.setdefault(alloc.get("employee"), []).append(alloc)

    leave_map = {}
    for leave in leaves:
        leave_map.setdefault(leave.get("employee"), []).append(leave)

    return employees, allocation_map, leave_map


def get_pm_details(slack, reports_to, mention_users=True):
    """Get the Slack ID and name for the Project Manager"""
    if not reports_to:
        return None, "N/A"

    pm_user_id = frappe.db.get_value("Employee", reports_to, "user_id")
    pm_name = frappe.db.get_value("Employee", reports_to, "employee_name")

    slack_id = None
    if pm_user_id and mention_users:
        slack_id = slack.get_slack_user_id(employee_id=reports_to)

    return slack_id, pm_name or "N/A"


def get_mention_cell(slack_id, fallback_name):
    """Returns a rich_text user mention if slack_id is valid, otherwise returns a raw_text cell"""
    if slack_id:
        return {
            "type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": [{"type": "user", "user_id": slack_id}]}],
        }
    return {"type": "raw_text", "text": str(fallback_name)}


def send_blocks_in_chunks(slack, channel, blocks):
    """Slack limits messages to 50 blocks. Chunk and send if exceeded."""
    chunk_size = 50
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i : i + chunk_size]
        slack.slack_app.client.chat_postMessage(channel=channel, blocks=chunk)


def send_daily_workload_reminder():
    """Triggered daily. Alerts if today's allocation < 8 hours."""
    slack_settings = frappe.get_single("Slack Settings")
    if not slack_settings.send_daily_allocation_updates:
        return

    # Fail gracefully if enabled but PMS is missing
    if not (is_next_pms_installed() and IMPORT_SUCCESS):
        generate_error_log(
            "Daily Workload Reminder Error",
            message="Next PMS app is missing or failed to import, but 'Send Daily Allocation Updates' is enabled in Slack Settings.",
        )
        return

    date = getdate()
    if date.weekday() > 4:
        return

    target_channel = slack_settings.workload_channel_id or "#workload"
    mention_users = slack_settings.workload_mention_users

    slack = SlackIntegration()
    employees, allocation_map, leave_map = get_workload_data(date, date)

    underallocated_users = []

    for emp in employees:
        # Check for Holidays!
        if check_if_date_is_holiday(date, emp.name):
            continue

        leaves = leave_map.get(emp.name, [])
        # Check if they are on leave today
        on_leave = any(leave.get("from_date") <= date <= leave.get("to_date") for leave in leaves)
        if on_leave:
            continue

        allocs = [
            a
            for a in allocation_map.get(emp.name, [])
            if a.get("allocation_start_date") <= date <= a.get("allocation_end_date")
        ]
        total_allocated = sum(a.get("hours_allocated_per_day") or 0 for a in allocs)
        unallocated = max(0, STANDARD_HOURS - total_allocated)

        if unallocated > 0:
            user_slack_id = None
            if mention_users:
                user_slack_id = slack.get_slack_user_id(employee_id=emp.name)

            pm_slack_id, pm_name = get_pm_details(slack, emp.reports_to, mention_users)

            underallocated_users.append(
                {
                    "slack_id": user_slack_id,
                    "name": emp.employee_name,
                    "unallocated": unallocated,
                    "pm_slack_id": pm_slack_id,
                    "pm_name": pm_name,
                }
            )

    if not underallocated_users:
        return

    # Sort by highest hours unallocated
    underallocated_users.sort(key=lambda x: x["unallocated"], reverse=True)

    intro_blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":chart_with_upwards_trend: Daily Workload Alert", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "The following engineers have less than 8 hours allocated today:"},
        },
    ]

    header_row = [
        {"type": "raw_text", "text": "Engineer"},
        {"type": "raw_text", "text": "Unallocated Hours"},
        {"type": "raw_text", "text": "Project Manager"},
    ]

    chunk_size = 90
    first_message = True
    total_chunks = (len(underallocated_users) + chunk_size - 1) // chunk_size

    for i in range(0, len(underallocated_users), chunk_size):
        chunk = underallocated_users[i : i + chunk_size]
        rows = [header_row]

        for u in chunk:
            rows.append(
                [
                    get_mention_cell(u.get("slack_id"), u.get("name")),
                    {"type": "raw_text", "text": f"{u['unallocated']:g}h"},
                    get_mention_cell(u.get("pm_slack_id"), u.get("pm_name")),
                ]
            )

        table_block = {
            "type": "table",
            "column_settings": [
                {"is_wrapped": True},  # Engineer
                {"align": "center"},  # Unallocated Hours
                {"is_wrapped": True},  # Project Manager
            ],
            "rows": rows,
        }

        # Include header texts only on the first payload sent to slack
        payload_blocks = intro_blocks.copy() if first_message else []
        payload_blocks.append(table_block)

        # Include footer texts only on the last payload sent to slack
        if i // chunk_size == total_chunks - 1:
            payload_blocks.append({"type": "divider"})
            payload_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": ":bulb: _Please ensure all allocations are updated in the PMS system._",
                        }
                    ],
                }
            )

        slack.slack_app.client.chat_postMessage(channel=target_channel, blocks=payload_blocks)
        first_message = False


def send_weekly_workload_reminder():
    """Triggered weekly. Generates a table of underallocated hours for the week."""
    slack_settings = frappe.get_single("Slack Settings")
    if not slack_settings.send_weekly_allocation_updates:
        return

    # Fail gracefully if enabled but PMS is missing
    if not (is_next_pms_installed() and IMPORT_SUCCESS):
        generate_error_log(
            "Weekly Workload Reminder Error",
            message="Next PMS app is missing or failed to import, but 'Send Weekly Allocation Updates' is enabled in Slack Settings.",
        )
        return

    timesheet_settings = frappe.get_single("Timesheet Settings")
    date = getdate()

    # Check if today matches the scheduled run day (e.g. "Monday")
    if get_weekday(date) != timesheet_settings.remind_on:
        return

    target_channel = slack_settings.workload_channel_id or "#workload"
    mention_users = slack_settings.workload_mention_users

    # Establish the Monday to Friday for the evaluated week
    weekday = date.weekday()
    if weekday > 4:  # Run on weekend -> evaluates the next week
        monday = add_days(date, (7 - weekday) % 7)
    else:  # Run on weekday -> evaluates the current week
        monday = add_days(date, -weekday)

    end_date = add_days(monday, 4)  # Friday

    slack = SlackIntegration()
    employees, allocation_map, leave_map = get_workload_data(monday, end_date)

    table_data = []

    for emp in employees:
        leaves = leave_map.get(emp.name, [])
        allocs = allocation_map.get(emp.name, [])

        day_unallocated = []
        has_underallocation = False

        for i in range(5):  # Mon to Fri
            cur_date = add_days(monday, i)

            # Check for Holidays!
            if check_if_date_is_holiday(cur_date, emp.name):
                day_unallocated.append(0)
                continue

            on_leave = any(leave.get("from_date") <= cur_date <= leave.get("to_date") for leave in leaves)

            if on_leave:
                day_unallocated.append(0)
                continue

            cur_allocs = [
                a for a in allocs if a.get("allocation_start_date") <= cur_date <= a.get("allocation_end_date")
            ]
            total_allocated = sum(a.get("hours_allocated_per_day") or 0 for a in cur_allocs)

            unallocated = max(0, STANDARD_HOURS - total_allocated)
            day_unallocated.append(unallocated)

            if unallocated > 0:
                has_underallocation = True

        if has_underallocation:
            user_slack_id = None
            if mention_users:
                user_slack_id = slack.get_slack_user_id(employee_id=emp.name)

            pm_slack_id, pm_name = get_pm_details(slack, emp.reports_to, mention_users)

            table_data.append(
                {
                    "slack_id": user_slack_id,
                    "name": emp.employee_name,
                    "days": day_unallocated,
                    "pm_slack_id": pm_slack_id,
                    "pm_name": pm_name,
                }
            )

    if not table_data:
        return

    # Sort by highest total missing hours across the entire week
    table_data.sort(key=lambda x: sum(x["days"]), reverse=True)

    intro_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ":date: Weekly Workload Alert", "emoji": True}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "The following engineers have less than 8 hours allocated this week:"},
        },
    ]

    header_row = [
        {"type": "raw_text", "text": "Engineer"},
        {"type": "raw_text", "text": "Mon"},
        {"type": "raw_text", "text": "Tue"},
        {"type": "raw_text", "text": "Wed"},
        {"type": "raw_text", "text": "Thu"},
        {"type": "raw_text", "text": "Fri"},
        {"type": "raw_text", "text": "Project Manager"},
    ]

    chunk_size = 90
    first_message = True
    total_chunks = (len(table_data) + chunk_size - 1) // chunk_size

    for i in range(0, len(table_data), chunk_size):
        chunk = table_data[i : i + chunk_size]
        rows = [header_row]

        for d in chunk:
            row = [get_mention_cell(d.get("slack_id"), d.get("name"))]
            for u in d["days"]:
                val = f"{u:g}h" if u > 0 else "-"
                row.append({"type": "raw_text", "text": val})

            row.append(get_mention_cell(d.get("pm_slack_id"), d.get("pm_name")))
            rows.append(row)

        table_block = {
            "type": "table",
            "column_settings": [
                {"is_wrapped": True},  # Engineer
                {"align": "center"},  # Mon
                {"align": "center"},  # Tue
                {"align": "center"},  # Wed
                {"align": "center"},  # Thu
                {"align": "center"},  # Fri
                {"is_wrapped": True},  # Project Manager
            ],
            "rows": rows,
        }

        # Include header texts only on the first payload sent to slack
        payload_blocks = intro_blocks.copy() if first_message else []
        payload_blocks.append(table_block)

        # Include footer texts only on the last payload sent to slack
        if i // chunk_size == total_chunks - 1:
            payload_blocks.append({"type": "divider"})
            payload_blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": ":bulb: _Values shown are unallocated hours per day._"}],
                }
            )

        slack.slack_app.client.chat_postMessage(channel=target_channel, blocks=payload_blocks)
        first_message = False

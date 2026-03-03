import frappe
from frappe import _ as translate
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
            translate(
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
    """Get the Slack ID and name for the Reporting Manager"""
    if not reports_to:
        return None, "N/A"

    pm_user_id = frappe.db.get_value("Employee", reports_to, "user_id")
    pm_name = frappe.db.get_value("Employee", reports_to, "employee_name")

    slack_id = None
    if pm_user_id and mention_users:
        slack_id = slack.get_slack_user_id(employee_id=reports_to)

    return slack_id, pm_name or "N/A"


def get_mention_text(slack_id, fallback_name):
    """Returns a string formatted for markdown with the user's name and slack mention (Used for Daily List)."""
    if slack_id:
        return f"{fallback_name} (<@{slack_id}>)"
    return str(fallback_name)


def get_mention_cell(slack_id, fallback_name, include_name=False):
    """Returns a rich_text user mention for the Slack table block (Used for Weekly Table)."""
    if slack_id:
        elements = []
        if include_name:
            elements.append({"type": "text", "text": f"{fallback_name} ("})

        elements.append({"type": "user", "user_id": slack_id})

        if include_name:
            elements.append({"type": "text", "text": ")"})

        return {
            "type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": elements}],
        }
    return {"type": "raw_text", "text": str(fallback_name)}


def send_blocks_in_chunks(slack, channel, blocks):
    """Slack limits messages to 50 blocks. Chunk and send if exceeded."""
    chunk_size = 50
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i : i + chunk_size]
        slack.slack_app.client.chat_postMessage(channel=channel, blocks=chunk)


# ==========================================
# DAILY WORKLOAD REMINDER (Markdown List)
# ==========================================


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
        if check_if_date_is_holiday(date, emp.name):
            continue

        leaves = leave_map.get(emp.name, [])
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
            if mention_users and emp.user_id:
                user_slack_id = frappe.db.get_value("User Meta", {"user": emp.user_id}, "custom_slack_userid")

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

    # Group users by Reporting Manager and aggregate their unallocated time
    grouped_data = {}
    for u in underallocated_users:
        pm = u["pm_name"]
        if pm not in grouped_data:
            grouped_data[pm] = {"pm_slack_id": u["pm_slack_id"], "engineers": [], "total_unallocated": 0}
        grouped_data[pm]["engineers"].append(u)
        grouped_data[pm]["total_unallocated"] += u["unallocated"]

    # Sort managers by highest total unallocated time
    sorted_managers = sorted(grouped_data.items(), key=lambda x: x[1]["total_unallocated"], reverse=True)

    # Sort engineers within managers by unallocated time
    for _, data in sorted_managers:
        data["engineers"].sort(key=lambda x: x["unallocated"], reverse=True)

    section_texts = format_daily_workload_groups(sorted_managers)
    blocks = format_daily_workload_blocks(len(underallocated_users), section_texts)

    send_blocks_in_chunks(slack, target_channel, blocks)


def format_daily_workload_groups(sorted_managers: list) -> list:
    """Format daily groups into chunked text strings (below Slack's 3000 char limit)."""
    sections = []
    current_text = ""

    for pm_name, data in sorted_managers:
        pm_mention = get_mention_text(data["pm_slack_id"], pm_name)
        pm_text = f"*{pm_mention}*\n"

        for index, emp in enumerate(data["engineers"], start=1):
            eng_mention = get_mention_text(emp["slack_id"], emp["name"])
            emp_text = f"  {index}. {eng_mention} - _{emp['unallocated']:g}h_\n"

            # Check if adding this will exceed Slack's limit
            if len(current_text) + len(pm_text) + len(emp_text) > 2900:
                if current_text:
                    sections.append(current_text.strip())
                    current_text = ""
                pm_text = f"*{pm_mention}* _(cont.)_\n"

            pm_text += emp_text

        current_text += pm_text + "\n"

    if current_text:
        sections.append(current_text.strip())

    return sections


def format_daily_workload_blocks(employee_count: int, section_texts: list) -> list:
    """Format the daily workload summary into Slack blocks."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":chart_with_upwards_trend: {employee_count} Underallocated Engineer{'s' if employee_count > 1 else ''} Today",
                "emoji": True,
            },
        },
    ]

    for text in section_texts:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text,
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
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

    return blocks


# ==========================================
# WEEKLY WORKLOAD REMINDER (Table Format)
# ==========================================


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
            if mention_users and emp.user_id:
                user_slack_id = frappe.db.get_value("User Meta", {"user": emp.user_id}, "custom_slack_userid")

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

    # Group by Reporting Manager alphabetically, then sort by highest unallocated hours
    table_data.sort(key=lambda x: (x["pm_name"], -sum(x["days"])))

    intro_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ":date: Weekly Workload Alert", "emoji": True}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "The following engineers have less than 8 hours allocated this week:"},
        },
    ]

    # Build dynamic headers with dates (e.g. "Mon (Oct 14)")
    header_row = [{"type": "raw_text", "text": "Engineer"}]
    for i in range(5):
        cur_date = getdate(add_days(monday, i))
        # Use strftime to get the abbreviated day and month/date format
        header_row.append({"type": "raw_text", "text": cur_date.strftime("%a (%b %d)")})
    header_row.append({"type": "raw_text", "text": "Reporting Manager"})

    chunk_size = 90
    first_message = True
    total_chunks = (len(table_data) + chunk_size - 1) // chunk_size

    for i in range(0, len(table_data), chunk_size):
        chunk = table_data[i : i + chunk_size]
        rows = [header_row]

        for d in chunk:
            # First column: Name (@handle)
            row = [get_mention_cell(d.get("slack_id"), d.get("name"), include_name=True)]

            for u in d["days"]:
                val = f"{u:g}h" if u > 0 else "-"
                row.append({"type": "raw_text", "text": val})

            # Last column: Reporting Manager (repeats on every row)
            row.append(get_mention_cell(d.get("pm_slack_id"), d.get("pm_name"), include_name=False))
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
                {"is_wrapped": True},  # Reporting Manager
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

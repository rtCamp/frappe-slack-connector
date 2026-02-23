import frappe
from frappe import _
from frappe.utils import add_days, getdate

try:
    from next_pms.resource_management.api.utils.query import (
        get_allocation_list_for_employee_for_given_range,
        get_employee_leaves,
    )
except ImportError:

    def get_allocation_list_for_employee_for_given_range(*args, **kwargs):
        frappe.throw(
            _(
                "get_allocation_list_for_employee_for_given_range is not implemented because next_pms module is missing."
            ),
            exc=NotImplementedError,
        )

    def get_employee_leaves(*args, **kwargs):
        frappe.throw(
            _(
                "get_allocation_list_for_employee_for_given_range is not implemented because next_pms module is missing."
            ),
            exc=NotImplementedError,
        )


from frappe_slack_connector.slack.app import SlackIntegration

SKIP_WEEKENDS = [5, 6]  # Saturday and Sunday
STANDARD_HOURS = 8
TARGET_CHANNEL = "#workload"


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

    # Bypass `get_employee_leaves` to avoid SQL tuple formatting errors
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


def get_pm_details(slack, reports_to):
    """Get the Slack ID and name for the Project Manager"""
    if not reports_to:
        return None, "N/A"

    pm_user_id = frappe.db.get_value("Employee", reports_to, "user_id")
    pm_name = frappe.db.get_value("Employee", reports_to, "employee_name")

    slack_id = None
    if pm_user_id:
        slack_id = slack.get_slack_user_id(employee_id=reports_to, from_api=False)

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
    date = getdate()
    if date.weekday() in SKIP_WEEKENDS:
        return

    slack = SlackIntegration()
    employees, allocation_map, leave_map = get_workload_data(date, date)

    underallocated_users = []

    for emp in employees:
        leaves = leave_map.get(emp.name, [])
        # Check if they are on leave today
        on_leave = any(l.get("from_date") <= date <= l.get("to_date") for l in leaves)
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
            user_slack_id = slack.get_slack_user_id(employee_id=emp.name, from_api=False)
            pm_slack_id, pm_name = get_pm_details(slack, emp.reports_to)

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
        {"type": "header", "text": {"type": "plain_text", "text": "📉 Daily Workload Alert", "emoji": True}},
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

    # Slack restricts tables to a maximum of 100 rows and only 1 table per message.
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
                        {"type": "mrkdwn", "text": "💡 _Please ensure all allocations are updated in the PMS system._"}
                    ],
                }
            )

        slack.slack_app.client.chat_postMessage(channel=TARGET_CHANNEL, blocks=payload_blocks)
        first_message = False


def send_weekly_workload_reminder():
    """Triggered every Monday. Generates a table of underallocated hours for the week."""
    date = getdate()
    # if date.weekday() != 0:  # Proceed only if today is Monday
    #     return

    end_date = add_days(date, 4)  # Friday
    slack = SlackIntegration()
    employees, allocation_map, leave_map = get_workload_data(date, end_date)

    table_data = []

    for emp in employees:
        leaves = leave_map.get(emp.name, [])
        allocs = allocation_map.get(emp.name, [])

        day_unallocated = []
        has_underallocation = False

        for i in range(5):  # Mon to Fri
            cur_date = add_days(date, i)
            on_leave = any(l.get("from_date") <= cur_date <= l.get("to_date") for l in leaves)

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
            user_slack_id = slack.get_slack_user_id(employee_id=emp.name, from_api=False)
            pm_slack_id, pm_name = get_pm_details(slack, emp.reports_to)

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

    intro_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🗓️ Weekly Workload Alert", "emoji": True}},
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

    # Slack restricts tables to a maximum of 100 rows and only 1 table per message.
    chunk_size = 90
    first_message = True
    total_chunks = (len(table_data) + chunk_size - 1) // chunk_size

    for i in range(0, len(table_data), chunk_size):
        chunk = table_data[i : i + chunk_size]
        rows = [header_row]

        for d in chunk:
            # Use get_mention_cell to correctly tag the engineer in the first column
            row = [get_mention_cell(d.get("slack_id"), d.get("name"))]
            for u in d["days"]:
                val = f"{u:g}h" if u > 0 else "-"
                row.append({"type": "raw_text", "text": val})

            # Use get_mention_cell to correctly tag the PM in the last column
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
                    "elements": [{"type": "mrkdwn", "text": "💡 _Values shown are unallocated hours per day._"}],
                }
            )

        slack.slack_app.client.chat_postMessage(channel=TARGET_CHANNEL, blocks=payload_blocks)
        first_message = False

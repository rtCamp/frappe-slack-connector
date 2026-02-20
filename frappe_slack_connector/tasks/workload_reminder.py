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
    # Assuming engineers are Active employees
    employees = frappe.get_all(
        "Employee", filters={"status": "Active"}, fields=["name", "employee_name", "reports_to", "user_id"]
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


def get_pm_mention(slack, reports_to):
    """Get the Slack mention for the Project Manager (reports_to)"""
    if not reports_to:
        return "N/A"

    pm_user_id = frappe.db.get_value("Employee", reports_to, "user_id")
    pm_name = frappe.db.get_value("Employee", reports_to, "employee_name")

    if pm_user_id:
        slack_id = slack.get_slack_user_id(employee_id=reports_to)
        if slack_id:
            return f"<@{slack_id}>"

    return pm_name


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
            user_slack_id = slack.get_slack_user_id(employee_id=emp.name)
            user_mention = f"<@{user_slack_id}>" if user_slack_id else emp.employee_name
            pm_mention = get_pm_mention(slack, emp.reports_to)

            underallocated_users.append({"name": user_mention, "unallocated": unallocated, "pm": pm_mention})

    if not underallocated_users:
        return

    # Sort by highest hours unallocated
    underallocated_users.sort(key=lambda x: x["unallocated"], reverse=True)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📉 Daily Workload Alert"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Engineers with less than 8 hours allocated today:*"}},
    ]

    # Chunk the message strings to avoid 3000 chars limit per block
    current_msg = ""
    for u in underallocated_users:
        line = f"• {u['name']}: *{u['unallocated']} hrs* unallocated (PM: {u['pm']})"
        if len(current_msg) + len(line) + 1 > 2900:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current_msg.strip()}})
            current_msg = line + "\n"
        else:
            current_msg += line + "\n"

    if current_msg.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current_msg.strip()}})

    send_blocks_in_chunks(slack, TARGET_CHANNEL, blocks)


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
            table_data.append({"name": emp.employee_name, "days": day_unallocated})

    if not table_data:
        return

    intro_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🗓️ Weekly Workload Alert"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Engineers with less than 8 hours allocated this week:*"},
        },
    ]

    header_row = [
        {"type": "raw_text", "text": "Engineer"},
        {"type": "raw_text", "text": "Mon"},
        {"type": "raw_text", "text": "Tue"},
        {"type": "raw_text", "text": "Wed"},
        {"type": "raw_text", "text": "Thu"},
        {"type": "raw_text", "text": "Fri"},
    ]

    # Slack restricts tables to a maximum of 100 rows and only 1 table per message.
    # We will chunk the data and send separate messages for each table to avoid 'only_one_table_allowed'.
    chunk_size = 90
    first_message = True

    for i in range(0, len(table_data), chunk_size):
        chunk = table_data[i : i + chunk_size]
        rows = [header_row]

        for d in chunk:
            row = [{"type": "raw_text", "text": d["name"]}]
            for u in d["days"]:
                val = f"{u:g}" if u > 0 else "-"
                row.append({"type": "raw_text", "text": val})
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
            ],
            "rows": rows,
        }

        # Include header texts only on the first payload sent to slack
        payload_blocks = intro_blocks.copy() if first_message else []
        payload_blocks.append(table_block)

        slack.slack_app.client.chat_postMessage(channel=TARGET_CHANNEL, blocks=payload_blocks)
        first_message = False

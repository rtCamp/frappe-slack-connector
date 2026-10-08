import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import today

# Custom fields (shipped in fixtures/custom_field.json) that remember the
# applicant's "Leave Request Submitted" Slack DM so it can be edited in place
# once the leave is approved, rejected or cancelled.
APPLICANT_CHANNEL_FIELD = "custom_slack_applicant_channel"
APPLICANT_MSG_TS_FIELD = "custom_slack_applicant_msg_ts"


def custom_fields_exist() -> bool:
    """
    Check if the custom fields for rtCamp exist in the Leave Application doctype
    """
    # Check if the custom fields exist in the Leave Application doctype
    return frappe.get_meta("Leave Application").has_field("custom_first_halfsecond_half")


def applicant_message_fields_exist() -> bool:
    """
    Check if the fields that store the applicant's Slack DM reference exist
    on the Leave Application doctype (they are added by this app's fixtures)
    """
    meta = frappe.get_meta("Leave Application")
    return meta.has_field(APPLICANT_CHANNEL_FIELD) and meta.has_field(APPLICANT_MSG_TS_FIELD)


def get_applicant_message_ref(leave_id: str) -> tuple[str | None, str | None]:
    """
    Return the (channel, ts) of the applicant's Slack DM for the given leave,
    or (None, None) when nothing is stored
    """
    if not applicant_message_fields_exist():
        return None, None
    row = frappe.db.get_value(
        "Leave Application",
        leave_id,
        [APPLICANT_CHANNEL_FIELD, APPLICANT_MSG_TS_FIELD],
        as_dict=True,
    )
    if not row:
        return None, None
    return row.get(APPLICANT_CHANNEL_FIELD) or None, row.get(APPLICANT_MSG_TS_FIELD) or None


def store_applicant_message_ref(leave_id: str, *, channel: str | None, ts: str | None) -> None:
    """
    Remember the applicant's Slack DM (channel, ts) on the Leave Application.

    Written directly to the database without touching ``modified`` so that
    storing the reference does not fire another ``on_update`` doc event.
    """
    if not (channel and ts) or not applicant_message_fields_exist():
        return
    frappe.db.set_value(
        "Leave Application",
        leave_id,
        {APPLICANT_CHANNEL_FIELD: channel, APPLICANT_MSG_TS_FIELD: ts},
        update_modified=False,
    )


def get_employees_on_leave() -> list:
    """
    Get all employees on leave today.

    Only employees whose status is ``Active`` are returned. A leave can be
    approved while the employee is still active and the employee may then
    leave the organisation before the leave date, so the Employee status is
    checked at query time rather than trusting the Leave Application alone.
    """
    current_date = today()

    fields = [
        "employee",
        "employee_name",
        "leave_type",
        "from_date",
        "to_date",
        "status",
        "half_day",
        "half_day_date",
    ]

    if custom_fields_exist():
        fields.append("custom_first_halfsecond_half")

    leave_applications = frappe.get_all(
        "Leave Application",
        filters={
            "from_date": ("<=", current_date),
            "to_date": (">=", current_date),
            "status": (
                "in",
                ["Open", "Approved"],
            ),
        },
        fields=fields,
        order_by="to_date asc",
    )

    if not leave_applications:
        return []

    # Filter on the (small) set of employees on leave today instead of
    # loading every active employee in the company into memory.
    active_employees = set(
        frappe.get_all(
            "Employee",
            filters={
                "name": ("in", {la.employee for la in leave_applications}),
                "status": "Active",
            },
            pluck="name",
        )
    )

    return [la for la in leave_applications if la.employee in active_employees]


def approve_leave(leave_id: str) -> None:
    """
    Approve the leave application
    """
    # Logic to approve the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    if custom_fields_exist():
        apply_workflow(leave_request, "Approve")
    else:
        leave_request.status = "Approved"
        leave_request.save()
        leave_request.submit()
    leave_request.add_comment(comment_type="Info", text="approved via Slack")


def reject_leave(leave_id: str) -> None:
    """
    Reject the leave application
    """
    # Logic to reject the leave request
    leave_request = frappe.get_doc("Leave Application", leave_id)
    if custom_fields_exist():
        apply_workflow(leave_request, "Reject")
    else:
        leave_request.status = "Rejected"
        leave_request.save()
        leave_request.submit()
    leave_request.add_comment(comment_type="Info", text="rejected via Slack")

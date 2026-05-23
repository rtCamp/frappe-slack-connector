import frappe
from frappe.model.workflow import apply_workflow
from frappe.query_builder import DocType
from frappe.utils import today


def custom_fields_exist() -> bool:
    """
    Check if the custom fields for rtCamp exist in the Leave Application doctype
    """
    # Check if the custom fields exist in the Leave Application doctype
    return frappe.get_meta("Leave Application").has_field("custom_first_halfsecond_half")


def get_employees_on_leave() -> list:
    """
    Get all active employees on leave today
    """
    current_date = today()

    LA = DocType("Leave Application")
    Emp = DocType("Employee")

    query = (
        frappe.qb.from_(LA)
        .inner_join(Emp)
        .on(LA.employee == Emp.name)
        .where(Emp.status == "Active")
        .where(LA.from_date <= current_date)
        .where(LA.to_date >= current_date)
        .where(LA.status.isin(["Open", "Approved"]))
        .where(LA.docstatus < 2)
        .select(
            LA.employee,
            LA.employee_name,
            LA.leave_type,
            LA.from_date,
            LA.to_date,
            LA.status,
            LA.half_day,
            LA.half_day_date,
        )
        .orderby(LA.to_date)
    )

    if custom_fields_exist():
        query = query.select(LA.custom_first_halfsecond_half)

    return query.run(as_dict=True)


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

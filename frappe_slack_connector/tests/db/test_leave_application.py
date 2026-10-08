from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from frappe_slack_connector.db.leave_application import (
    approve_leave,
    get_employees_on_leave,
    reject_leave,
)

LEAVE_DB_MODULE = "frappe_slack_connector.db.leave_application"


class TestApproveLeave(IntegrationTestCase):
    def test_applies_workflow_action_when_custom_field_exists(self):
        """approve_leave calls apply_workflow with 'Approve' when custom_first_halfsecond_half exists on Leave Application."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            approve_leave("HR-LAP-0001")
        mock_apply.assert_called_once_with(mock_leave, "Approve")
        mock_leave.save.assert_not_called()

    def test_sets_status_and_saves_and_submits_when_no_custom_field(self):
        """approve_leave sets status='Approved', saves, and submits when the custom field is absent."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            approve_leave("HR-LAP-0002")
        self.assertEqual(mock_leave.status, "Approved")
        mock_leave.save.assert_called_once()
        mock_leave.submit.assert_called_once()
        mock_apply.assert_not_called()

    def test_appends_approved_via_slack_comment(self):
        """approve_leave appends a Comment with text 'approved via Slack' after the approve operation."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
        ):
            approve_leave("HR-LAP-0003")
        mock_leave.add_comment.assert_called_once_with(comment_type="Info", text="approved via Slack")


class TestRejectLeave(IntegrationTestCase):
    def test_applies_workflow_action_when_custom_field_exists(self):
        """reject_leave calls apply_workflow with 'Reject' when custom_first_halfsecond_half exists."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            reject_leave("HR-LAP-0004")
        mock_apply.assert_called_once_with(mock_leave, "Reject")
        mock_leave.save.assert_not_called()

    def test_sets_status_and_saves_and_submits_when_no_custom_field(self):
        """reject_leave sets status='Rejected', saves, and submits when the custom field is absent."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{LEAVE_DB_MODULE}.apply_workflow") as mock_apply,
        ):
            reject_leave("HR-LAP-0005")
        self.assertEqual(mock_leave.status, "Rejected")
        mock_leave.save.assert_called_once()
        mock_leave.submit.assert_called_once()
        mock_apply.assert_not_called()

    def test_appends_rejected_via_slack_comment(self):
        """reject_leave appends a Comment with text 'rejected via Slack' after the reject operation."""
        mock_leave = MagicMock()
        with (
            patch(f"{LEAVE_DB_MODULE}.frappe.get_doc", return_value=mock_leave),
            patch(f"{LEAVE_DB_MODULE}.custom_fields_exist", return_value=False),
        ):
            reject_leave("HR-LAP-0006")
        mock_leave.add_comment.assert_called_once_with(comment_type="Info", text="rejected via Slack")


class TestGetEmployeesOnLeave(IntegrationTestCase):
    """Rows are written with ``db_insert`` so no Leave Application / Employee
    controller validation (allocations, approvers, holiday lists) runs. The
    function under test only reads the two tables, so that is all it needs."""

    ACTIVE_EMPLOYEE = "_T-FSC-EMP-ACTIVE"
    LEFT_EMPLOYEE = "_T-FSC-EMP-LEFT"
    OPEN_EMPLOYEE = "_T-FSC-EMP-OPEN"
    REJECTED_EMPLOYEE = "_T-FSC-EMP-REJECTED"
    CANCELLED_EMPLOYEE = "_T-FSC-EMP-CANCELLED"
    DISCARDED_EMPLOYEE = "_T-FSC-EMP-DISCARDED"
    FORCE_CANCELLED_EMPLOYEE = "_T-FSC-EMP-FORCE-CANCELLED"

    # (employee, employee status, leave status, leave docstatus), one leave
    # covering today per employee so each case can be asserted by employee.
    FIXTURES = (
        (ACTIVE_EMPLOYEE, "Active", "Approved", 1),
        (LEFT_EMPLOYEE, "Left", "Approved", 1),
        (OPEN_EMPLOYEE, "Active", "Open", 0),
        (REJECTED_EMPLOYEE, "Active", "Rejected", 1),
        # Submitted leave cancelled through the HRMS controller
        (CANCELLED_EMPLOYEE, "Active", "Cancelled", 2),
        # Draft leave discarded: HRMS on_discard sets status Cancelled
        (DISCARDED_EMPLOYEE, "Active", "Cancelled", 2),
        # Cancelled with flags.ignore_validate: before_cancel is skipped and
        # status stays Approved while docstatus becomes 2
        (FORCE_CANCELLED_EMPLOYEE, "Active", "Approved", 2),
    )

    @classmethod
    def setUpClass(cls):
        # Fixture rows are inserted once per class: IntegrationTestCase rolls
        # the database back at class teardown, not after every test.
        super().setUpClass()
        company = frappe.defaults.get_global_default("company")
        leave_type = frappe.get_all("Leave Type", pluck="name", limit=1)[0]
        day = today()

        for name, employee_status, leave_status, docstatus in cls.FIXTURES:
            frappe.get_doc(
                {
                    "doctype": "Employee",
                    "name": name,
                    "first_name": name,
                    "employee_name": name,
                    "status": employee_status,
                    "company": company,
                    "gender": "Other",
                    "date_of_birth": "1990-01-01",
                    "date_of_joining": "2020-01-01",
                    "relieving_date": day if employee_status == "Left" else None,
                }
            ).db_insert()

            frappe.get_doc(
                {
                    "doctype": "Leave Application",
                    "name": f"_T-FSC-LAP-{name.removeprefix('_T-FSC-EMP-')}",
                    "employee": name,
                    "employee_name": name,
                    "leave_type": leave_type,
                    "company": company,
                    "from_date": day,
                    "to_date": day,
                    "posting_date": day,
                    "status": leave_status,
                    "docstatus": docstatus,
                }
            ).db_insert()

    def test_excludes_employees_who_are_not_active(self):
        """An approved leave for an employee whose status is no longer Active must not be reported."""
        on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertIn(self.ACTIVE_EMPLOYEE, on_leave)
        self.assertNotIn(self.LEFT_EMPLOYEE, on_leave)

    def test_includes_open_and_approved_leave(self):
        """Leave is approved by default, so both Open (draft) and Approved (submitted) leave covering today is reported."""
        on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertIn(self.OPEN_EMPLOYEE, on_leave)
        self.assertIn(self.ACTIVE_EMPLOYEE, on_leave)

    def test_excludes_rejected_leave(self):
        """A submitted leave with status Rejected covering today is not reported, so a rebuilt summary drops the person."""
        on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertNotIn(self.REJECTED_EMPLOYEE, on_leave)

    def test_excludes_cancelled_and_discarded_leave(self):
        """A cancelled submitted leave and a discarded draft (both status Cancelled, docstatus 2) covering today are not reported."""
        on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertNotIn(self.CANCELLED_EMPLOYEE, on_leave)
        self.assertNotIn(self.DISCARDED_EMPLOYEE, on_leave)

    def test_excludes_cancelled_docstatus_even_when_status_still_approved(self):
        """A leave at docstatus 2 whose status was left at Approved (cancel with ignore_validate skips HRMS before_cancel) is excluded by the docstatus filter."""
        on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertNotIn(self.FORCE_CANCELLED_EMPLOYEE, on_leave)

    def test_returns_empty_list_when_nobody_is_on_leave(self):
        """When no Leave Application covers today the function returns [] without querying Employee."""
        with patch(f"{LEAVE_DB_MODULE}.frappe.get_all", return_value=[]) as mock_get_all:
            self.assertEqual(get_employees_on_leave(), [])
        mock_get_all.assert_called_once()
        self.assertEqual(mock_get_all.call_args.args[0], "Leave Application")
        filters = mock_get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["status"], ("in", ["Open", "Approved"]))
        self.assertEqual(filters["docstatus"], ("!=", 2))

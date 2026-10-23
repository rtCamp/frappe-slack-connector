from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from frappe_slack_connector.db.leave_application import (
    APPLICANT_CHANNEL_FIELD,
    APPLICANT_MSG_TS_FIELD,
    approve_leave,
    effective_leave_status,
    get_applicant_message_ref,
    get_employees_on_leave,
    get_leave_status,
    reject_leave,
    store_applicant_message_ref,
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

    def setUp(self):
        # The fixture employees have no holiday list, so the lookup falls back to
        # the company's; on a weekly off or a public holiday that would drop every
        # fixture employee and make these assertions vacuous. The holiday rule has
        # its own test below, which overrides this patch
        super().setUp()
        patcher = patch(f"{LEAVE_DB_MODULE}.get_employees_on_holiday", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _employees_on_leave(self) -> set[str]:
        on_leave = {row.employee for row in get_employees_on_leave()}
        # Positive control: an exclusion test must not pass because nothing was returned
        self.assertIn(self.ACTIVE_EMPLOYEE, on_leave)
        return on_leave

    def test_excludes_employees_who_are_not_active(self):
        """An approved leave for an employee whose status is no longer Active must not be reported."""
        self.assertNotIn(self.LEFT_EMPLOYEE, self._employees_on_leave())

    def test_includes_open_and_approved_leave(self):
        """Leave is approved by default, so both Open (draft) and Approved (submitted) leave covering today is reported."""
        on_leave = self._employees_on_leave()
        self.assertIn(self.OPEN_EMPLOYEE, on_leave)
        self.assertIn(self.ACTIVE_EMPLOYEE, on_leave)

    def test_excludes_rejected_leave(self):
        """A submitted leave with status Rejected covering today is not reported, so a rebuilt summary drops the person."""
        self.assertNotIn(self.REJECTED_EMPLOYEE, self._employees_on_leave())

    def test_excludes_cancelled_and_discarded_leave(self):
        """A cancelled submitted leave and a discarded draft (both status Cancelled, docstatus 2) covering today are not reported."""
        on_leave = self._employees_on_leave()
        self.assertNotIn(self.CANCELLED_EMPLOYEE, on_leave)
        self.assertNotIn(self.DISCARDED_EMPLOYEE, on_leave)

    def test_excludes_cancelled_docstatus_even_when_status_still_approved(self):
        """A leave at docstatus 2 whose status was left at Approved (cancel with ignore_validate skips HRMS before_cancel) is excluded by the docstatus filter."""
        self.assertNotIn(self.FORCE_CANCELLED_EMPLOYEE, self._employees_on_leave())

    def test_excludes_employees_for_whom_today_is_a_holiday(self):
        """An employee whose own holiday list marks today a holiday is not on leave today, even with a leave covering the date."""
        with patch(
            f"{LEAVE_DB_MODULE}.get_employees_on_holiday", return_value={self.ACTIVE_EMPLOYEE}
        ) as mock_on_holiday:
            on_leave = {row.employee for row in get_employees_on_leave()}
        self.assertNotIn(self.ACTIVE_EMPLOYEE, on_leave)
        # Only active employees are checked, and against today.
        mock_on_holiday.assert_called_once()
        checked, on_date = mock_on_holiday.call_args.args
        self.assertIn(self.ACTIVE_EMPLOYEE, checked)
        self.assertNotIn(self.LEFT_EMPLOYEE, checked)
        self.assertEqual(on_date, today())

    def test_returns_empty_list_when_nobody_is_on_leave(self):
        """When no Leave Application covers today the function returns [] without querying Employee."""
        with patch(f"{LEAVE_DB_MODULE}.frappe.get_all", return_value=[]) as mock_get_all:
            self.assertEqual(get_employees_on_leave(), [])
        mock_get_all.assert_called_once()
        self.assertEqual(mock_get_all.call_args.args[0], "Leave Application")
        filters = mock_get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["status"], ("in", ["Open", "Approved"]))
        self.assertEqual(filters["docstatus"], ("!=", 2))


class TestApplicantMessageRef(IntegrationTestCase):
    def test_store_writes_channel_and_ts_without_touching_modified(self):
        """store_applicant_message_ref writes both custom fields via db.set_value with update_modified=False so no doc event fires."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.frappe.db.set_value") as mock_set_value,
        ):
            store_applicant_message_ref("HR-LAP-0010", channel="D0FSC0001", ts="1700000000.000001")
        mock_set_value.assert_called_once_with(
            "Leave Application",
            "HR-LAP-0010",
            {APPLICANT_CHANNEL_FIELD: "D0FSC0001", APPLICANT_MSG_TS_FIELD: "1700000000.000001"},
            update_modified=False,
        )

    def test_store_skips_when_response_has_no_ts(self):
        """store_applicant_message_ref does nothing when channel or ts is missing."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.frappe.db.set_value") as mock_set_value,
        ):
            store_applicant_message_ref("HR-LAP-0011", channel="D0FSC0001", ts=None)
        mock_set_value.assert_not_called()

    def test_store_skips_when_custom_fields_are_not_installed(self):
        """store_applicant_message_ref does nothing when the custom fields are absent from Leave Application."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=False),
            patch(f"{LEAVE_DB_MODULE}.frappe.db.set_value") as mock_set_value,
        ):
            store_applicant_message_ref("HR-LAP-0012", channel="D0FSC0001", ts="1700000000.000001")
        mock_set_value.assert_not_called()

    def test_get_returns_stored_channel_and_ts(self):
        """get_applicant_message_ref returns the (channel, ts) pair stored on the Leave Application."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(
                f"{LEAVE_DB_MODULE}.frappe.db.get_value",
                return_value=frappe._dict(
                    {APPLICANT_CHANNEL_FIELD: "D0FSC0001", APPLICANT_MSG_TS_FIELD: "1700000000.000001"}
                ),
            ),
        ):
            self.assertEqual(get_applicant_message_ref("HR-LAP-0013"), ("D0FSC0001", "1700000000.000001"))

    def test_get_returns_none_pair_when_nothing_stored(self):
        """get_applicant_message_ref returns (None, None) when the fields are empty or the custom fields are absent."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(
                f"{LEAVE_DB_MODULE}.frappe.db.get_value",
                return_value=frappe._dict({APPLICANT_CHANNEL_FIELD: None, APPLICANT_MSG_TS_FIELD: None}),
            ),
        ):
            self.assertEqual(get_applicant_message_ref("HR-LAP-0014"), (None, None))
        with patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=False):
            self.assertEqual(get_applicant_message_ref("HR-LAP-0015"), (None, None))

    def test_get_returns_none_pair_for_missing_leave_id(self):
        """get_applicant_message_ref returns (None, None) without querying when the leave has no name."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.frappe.db.get_value") as mock_get_value,
        ):
            self.assertEqual(get_applicant_message_ref(None), (None, None))
            self.assertEqual(get_applicant_message_ref(""), (None, None))
        mock_get_value.assert_not_called()

    def test_get_returns_none_pair_when_row_is_missing(self):
        """get_applicant_message_ref returns (None, None) when the Leave Application row does not exist (get_value gives None or an empty result)."""
        for missing in (None, ()):
            with (
                patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
                patch(f"{LEAVE_DB_MODULE}.frappe.db.get_value", return_value=missing),
            ):
                self.assertEqual(get_applicant_message_ref("HR-LAP-MISSING"), (None, None))


class TestLeaveStatus(IntegrationTestCase):
    def test_cancelled_document_reads_as_cancelled_whatever_the_status_column_says(self):
        """A force-cancelled leave (docstatus 2, status still Approved) is Cancelled for the applicant DM; otherwise the status column stands."""
        self.assertEqual(effective_leave_status("Approved", 2), "Cancelled")
        self.assertEqual(effective_leave_status("Rejected", "2"), "Cancelled")
        self.assertEqual(effective_leave_status("Approved", 1), "Approved")
        self.assertEqual(effective_leave_status("Open", 0), "Open")
        self.assertIsNone(effective_leave_status(None, 0))

    def test_get_leave_status_reads_status_and_docstatus_together(self):
        """get_leave_status reads both columns in one query and applies the docstatus rule; a missing row gives None."""
        with patch(
            f"{LEAVE_DB_MODULE}.frappe.db.get_value",
            return_value=frappe._dict(status="Approved", docstatus=2),
        ) as mock_get_value:
            self.assertEqual(get_leave_status("HR-LAP-0001"), "Cancelled")
        mock_get_value.assert_called_once_with(
            "Leave Application", "HR-LAP-0001", ["status", "docstatus"], as_dict=True
        )
        with patch(f"{LEAVE_DB_MODULE}.frappe.db.get_value", return_value=None):
            self.assertIsNone(get_leave_status("HR-LAP-0001"))

    def test_locks_the_row_when_asked_to(self):
        """for_update=True reads the reference with a row lock, so a save holding it cannot be overtaken by the job's write; the default read takes no lock."""
        with (
            patch(f"{LEAVE_DB_MODULE}.applicant_message_fields_exist", return_value=True),
            patch(f"{LEAVE_DB_MODULE}.frappe.db.get_value", return_value=None) as mock_get_value,
        ):
            get_applicant_message_ref("HR-LAP-0001", for_update=True)
            self.assertTrue(mock_get_value.call_args.kwargs.get("for_update"))
            get_applicant_message_ref("HR-LAP-0001")
            self.assertNotIn("for_update", mock_get_value.call_args.kwargs)

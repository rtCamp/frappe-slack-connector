from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.db.timesheet import create_timesheet_detail

TIMESHEET_DB_MODULE = "frappe_slack_connector.db.timesheet"


class TestCreateTimesheetDetail(IntegrationTestCase):
    def test_creates_new_timesheet_when_parent_is_none(self):
        """create_timesheet_detail constructs a new Timesheet doc keyed to the employee when no parent is supplied."""
        mock_timesheet = MagicMock()
        with (
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_doc", return_value=mock_timesheet) as mock_get_doc,
            patch(f"{TIMESHEET_DB_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_value", return_value="PRJ-1"),
        ):
            create_timesheet_detail(
                date="2026-06-10",
                hours=2.0,
                description="work",
                task="TASK-1",
                employee="EMP-001",
                parent=None,
            )
        # Construction path: get_doc called with a new-doc dict containing employee.
        mock_get_doc.assert_called_once_with({"doctype": "Timesheet", "employee": "EMP-001"})
        mock_timesheet.save.assert_called_once()

    def test_fetches_existing_timesheet_when_parent_is_supplied(self):
        """create_timesheet_detail fetches the existing Timesheet by name when parent is supplied."""
        mock_timesheet = MagicMock()
        with (
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_doc", return_value=mock_timesheet) as mock_get_doc,
            patch(f"{TIMESHEET_DB_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_value", return_value="PRJ-1"),
        ):
            create_timesheet_detail(
                date="2026-06-10",
                hours=2.0,
                description="work",
                task="TASK-1",
                employee="EMP-001",
                parent="TS-PARENT-1",
            )
        mock_get_doc.assert_called_once_with("Timesheet", "TS-PARENT-1")
        mock_timesheet.save.assert_called_once()

    def test_appends_time_log_row_with_from_time_and_to_time_based_on_hours(self):
        """The appended time_logs entry has from_time and to_time spaced by the supplied hours."""
        mock_timesheet = MagicMock()
        with (
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_doc", return_value=mock_timesheet),
            patch(f"{TIMESHEET_DB_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_value", return_value="PRJ-1"),
        ):
            create_timesheet_detail(
                date="2026-06-10 09:00:00",
                hours=2.5,
                description="work",
                task="TASK-1",
                employee="EMP-001",
            )
        mock_timesheet.append.assert_called_once()
        args = mock_timesheet.append.call_args.args
        self.assertEqual(args[0], "time_logs")
        log = args[1]
        self.assertEqual(log["task"], "TASK-1")
        self.assertEqual(log["description"], "work")
        self.assertEqual(log["hours"], 2.5)
        # to_time - from_time should equal 2.5 hours.
        diff = log["to_time"] - log["from_time"]
        self.assertEqual(diff.total_seconds() / 3600, 2.5)

    def test_stores_project_and_custom_is_billable_when_next_pms_installed(self):
        """When next_pms is installed, the time log row includes project and is_billable read off the Task."""
        mock_timesheet = MagicMock()
        with (
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_doc", return_value=mock_timesheet),
            patch(f"{TIMESHEET_DB_MODULE}.is_next_pms_installed", return_value=True),
            patch(
                f"{TIMESHEET_DB_MODULE}.frappe.get_value",
                return_value=("PRJ-PMS", 1),
            ),
        ):
            create_timesheet_detail(
                date="2026-06-10",
                hours=1.0,
                description="work",
                task="TASK-1",
                employee="EMP-001",
            )
        log = mock_timesheet.append.call_args.args[1]
        self.assertEqual(log["project"], "PRJ-PMS")
        self.assertEqual(log["is_billable"], 1)

    def test_stores_only_project_when_next_pms_not_installed(self):
        """When next_pms is not installed, the time log row includes project only (no is_billable)."""
        mock_timesheet = MagicMock()
        with (
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_doc", return_value=mock_timesheet),
            patch(f"{TIMESHEET_DB_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{TIMESHEET_DB_MODULE}.frappe.get_value", return_value="PRJ-1"),
        ):
            create_timesheet_detail(
                date="2026-06-10",
                hours=1.0,
                description="work",
                task="TASK-1",
                employee="EMP-001",
            )
        log = mock_timesheet.append.call_args.args[1]
        self.assertEqual(log["project"], "PRJ-1")
        self.assertNotIn("is_billable", log)

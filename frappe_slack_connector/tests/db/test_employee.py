from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.db.employee import (
    get_default_holiday_list,
    get_employees_on_holiday,
    is_company_holiday,
)

EMPLOYEE_DB_MODULE = "frappe_slack_connector.db.employee"


class TestIsCompanyHoliday(IntegrationTestCase):
    def test_checks_the_date_against_the_default_company_holiday_list(self):
        """is_company_holiday resolves the default company's holiday list and passes it, with the date, to erpnext's is_holiday."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.get_default_holiday_list", return_value="Company Holidays 2026"),
            patch(f"{EMPLOYEE_DB_MODULE}.is_holiday", return_value=True) as mock_is_holiday,
        ):
            self.assertTrue(is_company_holiday("2026-06-15"))
        mock_is_holiday.assert_called_once_with("Company Holidays 2026", "2026-06-15")

    def test_is_not_a_holiday_without_a_holiday_list(self):
        """is_company_holiday returns False, without querying, when no holiday list is configured."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.get_default_holiday_list", return_value=None),
            patch(f"{EMPLOYEE_DB_MODULE}.is_holiday") as mock_is_holiday,
        ):
            self.assertFalse(is_company_holiday("2026-06-15"))
        mock_is_holiday.assert_not_called()


class TestGetDefaultHolidayList(IntegrationTestCase):
    def test_returns_the_default_company_holiday_list(self):
        """get_default_holiday_list reads Company.default_holiday_list of the global default company."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.defaults.get_global_default", return_value="Acme") as mock_default,
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_cached_value", return_value="Acme Holidays") as mock_cached,
        ):
            self.assertEqual(get_default_holiday_list(), "Acme Holidays")
        # Global Defaults saves its default_company field under the "company" defaults key.
        mock_default.assert_called_once_with("company")
        mock_cached.assert_called_once_with("Company", "Acme", "default_holiday_list")

    def test_returns_none_without_a_default_company(self):
        """get_default_holiday_list returns None, without a Company lookup, when no default company is set."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.defaults.get_global_default", return_value=None),
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_cached_value") as mock_cached,
        ):
            self.assertIsNone(get_default_holiday_list())
        mock_cached.assert_not_called()


class TestGetEmployeesOnHoliday(IntegrationTestCase):
    def test_returns_only_employees_whose_own_list_has_the_date(self):
        """Employees on a list that marks the date a holiday are returned; employees on other lists, or on no list, are not. Holiday rows are fetched in one query across the distinct lists."""
        lists = {"EMP-IN": "[Time off - IN]", "EMP-SUPPORT": "[Time off - Support]", "EMP-IN-2": "[Time off - IN]"}
        with (
            patch(
                f"{EMPLOYEE_DB_MODULE}.get_holiday_list_for_employee",
                side_effect=lambda emp, raise_exception, as_on: lists.get(emp),
            ) as mock_list,
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_all", return_value=["[Time off - IN]"]) as mock_get_all,
        ):
            result = get_employees_on_holiday(["EMP-IN", "EMP-SUPPORT", "EMP-IN-2", "EMP-NOLIST"], "2026-10-02")

        self.assertEqual(result, {"EMP-IN", "EMP-IN-2"})
        self.assertEqual(mock_list.call_count, 4)
        mock_list.assert_any_call("EMP-IN", raise_exception=False, as_on="2026-10-02")
        mock_get_all.assert_called_once()
        self.assertEqual(mock_get_all.call_args.args[0], "Holiday")
        filters = mock_get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["holiday_date"], "2026-10-02")
        self.assertEqual(filters["parent"][0], "in")
        self.assertEqual(set(filters["parent"][1]), {"[Time off - IN]", "[Time off - Support]"})
        self.assertEqual(mock_get_all.call_args.kwargs["pluck"], "parent")

    def test_returns_empty_set_without_employees(self):
        """No employees means no lookups at all."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.get_holiday_list_for_employee") as mock_list,
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_all") as mock_get_all,
        ):
            self.assertEqual(get_employees_on_holiday([], "2026-10-02"), set())
        mock_list.assert_not_called()
        mock_get_all.assert_not_called()

    def test_returns_empty_set_when_nobody_has_a_holiday_list(self):
        """Employees without any holiday list (own or company) are never on holiday and the Holiday table is not queried."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.get_holiday_list_for_employee", return_value=None),
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_all") as mock_get_all,
        ):
            self.assertEqual(get_employees_on_holiday(["EMP-1", "EMP-2"], "2026-10-02"), set())
        mock_get_all.assert_not_called()

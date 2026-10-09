from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.db.employee import get_default_holiday_list, is_company_holiday

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
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.defaults.get_global_default", return_value="Acme"),
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_cached_value", return_value="Acme Holidays") as mock_cached,
        ):
            self.assertEqual(get_default_holiday_list(), "Acme Holidays")
        mock_cached.assert_called_once_with("Company", "Acme", "default_holiday_list")

    def test_returns_none_without_a_default_company(self):
        """get_default_holiday_list returns None, without a Company lookup, when no default company is set."""
        with (
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.defaults.get_global_default", return_value=None),
            patch(f"{EMPLOYEE_DB_MODULE}.frappe.get_cached_value") as mock_cached,
        ):
            self.assertIsNone(get_default_holiday_list())
        mock_cached.assert_not_called()

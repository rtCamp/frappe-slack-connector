from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.gmail_mirror import MIRROR, SETTING, is_enabled, is_gmail_thread_installed
from frappe_slack_connector.tests.gmail_mirror import set_mirroring

PKG = "frappe_slack_connector.gmail_mirror"


class TestGate(IntegrationTestCase):
    def test_off_by_default(self):
        self.assertEqual(frappe.get_meta("Slack Settings").get_field(SETTING).default, "0")

    def test_enabled_needs_app_and_switch(self):
        with patch(f"{PKG}.is_gmail_thread_installed", return_value=True):
            set_mirroring(True)
            self.assertTrue(is_enabled())
            set_mirroring(False)
            self.assertFalse(is_enabled())

    def test_disabled_without_the_gmail_app(self):
        set_mirroring(True)
        with patch(f"{PKG}.is_gmail_thread_installed", return_value=False):
            self.assertFalse(is_enabled())

    def test_installed_check_reads_the_site_app_list(self):
        with patch(f"{PKG}.frappe.get_installed_apps", return_value=["frappe", "frappe_gmail_thread"]):
            self.assertTrue(is_gmail_thread_installed())
        with patch(f"{PKG}.frappe.get_installed_apps", return_value=["frappe"]):
            self.assertFalse(is_gmail_thread_installed())

    def test_mirror_doctype_is_not_linked_to_gmail_thread(self):
        """A Link field would make the Gmail app a hard dependency; rows are written by the feature only."""
        meta = frappe.get_meta(MIRROR)
        self.assertEqual(meta.get_field("gmail_thread").fieldtype, "Data")
        self.assertFalse([p for p in meta.permissions if p.write or p.create])

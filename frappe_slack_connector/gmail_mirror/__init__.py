"""
Mirror an email thread of the Frappe Gmail Thread app into a Slack thread.

That app is optional: nothing here imports it, and every entry point checks
``is_enabled()`` first.
"""

import frappe

MIRROR = "Slack Thread Mirror"
MIRROR_MESSAGE = "Slack Thread Mirror Message"
SETTING = "enable_gmail_thread_mirroring"


def is_gmail_thread_installed() -> bool:
    return "frappe_gmail_thread" in frappe.get_installed_apps()


def is_switched_on() -> bool:
    return bool(frappe.db.get_single_value("Slack Settings", SETTING))


def is_enabled() -> bool:
    """Whether the Gmail Thread app is installed and mirroring is switched on in Slack Settings."""
    return is_gmail_thread_installed() and is_switched_on()

import frappe


def standard_date_fmt(date: str) -> str:
    """
    Format date to standard format
    """
    return frappe.utils.get_datetime(date).strftime("%b %d, %Y (%a)")

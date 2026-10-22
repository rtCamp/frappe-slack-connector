import traceback

import frappe


def generate_error_log(
    title: str,
    *,
    message: str = "",
    exception: Exception | None = None,
    msgprint: bool = False,
    realtime: bool = False,
):
    """
    Generate an error log with the given title and message
    """
    # The traceback, not just str(exception): frappe.log_error stores the message
    # it is given, and a bare "Not permitted" names no file or line
    exc = "".join(traceback.format_exception(exception)) if exception else frappe.get_traceback()
    frappe.log_error(
        title="Slack: " + title,
        message=f"{message}\nException:\n{exc}" if exception else message,
    )

    if msgprint:
        frappe.msgprint(
            title=title,
            msg=message,
            indicator="red",
            realtime=realtime,
        )

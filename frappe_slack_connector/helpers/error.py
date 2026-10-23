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


def exception_message(exception: Exception) -> str:
    """
    Text to show a user for an exception: its own message, or, when it has
    none, the last message Frappe queued for the client. A failed permission
    check raises a bare PermissionError and leaves the explanation in
    frappe.msgprint, so str(exception) alone renders an empty error box
    """
    from frappe_slack_connector.helpers.str_utils import strip_html_tags

    text = strip_html_tags(str(exception)).strip()
    if text:
        return text
    for entry in reversed(frappe.get_message_log()):
        message = strip_html_tags(str(entry.get("message") or "")).strip()
        if message:
            return message
    return type(exception).__name__

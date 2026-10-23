import traceback

import frappe
from frappe.utils import strip_html_tags


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
    if exception:
        exc = "".join(traceback.format_exception(exception))
        message = f"{message}\nException:\n{exc}"
    frappe.log_error(title="Slack: " + title, message=message)

    if msgprint:
        frappe.msgprint(
            title=title,
            msg=message,
            indicator="red",
            realtime=realtime,
        )


def _clean_message(text) -> str:
    """
    Plain text for a Frappe message: lists joined, ``<br>`` kept as line
    breaks, other HTML stripped
    """
    if isinstance(text, list | tuple):
        text = "\n".join(str(t) for t in text)
    text = str(text or "")
    for br in ("<br>", "<br/>", "<br />"):
        text = text.replace(br, "\n")
    return strip_html_tags(text).strip()


def exception_message(exception: Exception) -> str:
    """
    Text to show a user for an exception: its own message, or, for a
    permission failure, the reason Frappe recorded (``flags.error_message``,
    or the last message it queued for the client), or the exception type.
    Frappe's permission checks raise a bare PermissionError and leave the
    explanation in ``msgprint``, so ``str(exception)`` alone renders an
    empty error box. The queued messages are consulted only for permission
    errors: an unrelated earlier message ("Email sent to ...") must not be
    presented as the reason for some other failure
    """
    text = _clean_message(str(exception))
    if text:
        return text
    if isinstance(exception, frappe.PermissionError):
        for entry in reversed(frappe.get_message_log()):
            text = _clean_message(entry.get("message"))
            if text:
                return text
        text = _clean_message(frappe.flags.get("error_message"))
        if text:
            return text
    return type(exception).__name__

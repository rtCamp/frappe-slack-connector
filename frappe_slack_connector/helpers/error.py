import html
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
    # The traceback goes to Error Log only: a msgprint reaches a Desk user
    # (the "Test channel" button, the realtime attendance error), who gets the
    # message and the exception text as before, not server paths and frames
    log_message = message
    if exception:
        exc_text = str(exception) or type(exception).__name__
        exc_trace = "".join(traceback.format_exception(exception))
        log_message = "\n".join(part for part in (message, "Exception:", exc_trace) if part)
        message = "\n".join(part for part in (message, "Exception:", exc_text) if part)
    frappe.log_error(title="Slack: " + title, message=log_message)

    if msgprint:
        frappe.msgprint(
            title=title,
            msg=message,
            indicator="red",
            realtime=realtime,
        )


# Slack section text is capped at 3000 characters; leave room for the label and fences
MESSAGE_MAX_CHARS = 2900


def _clean_message(text) -> str:
    """
    Plain text for a Frappe message: lists joined, ``<br>`` kept as line
    breaks, other HTML stripped, entities unescaped, length capped
    """
    if isinstance(text, list | tuple):
        text = "\n".join(str(t) for t in text)
    text = str(text or "")
    for br in ("<br>", "<br/>", "<br />"):
        text = text.replace(br, "\n")
    # Frappe's msgprint HTML-encodes its text; decode it, then re-encode the
    # three characters Slack reads as control characters (&, <, >) so a
    # document name or user string can never become a mention or a link.
    # Slack decodes those entities for display, so "Tom & Jerry" still reads
    text = html.unescape(strip_html_tags(text)).strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(text) > MESSAGE_MAX_CHARS:
        text = text[: MESSAGE_MAX_CHARS - 1] + "…"
    return text


def exception_message(exception: Exception, *, since: int = 0) -> str:
    """
    Text to show a user for an exception: its own message, or, for a
    permission failure, the reason Frappe recorded (the last message it
    queued for the client, then ``flags.error_message``), or the exception
    type. Frappe's permission checks raise a bare PermissionError and leave
    the explanation in ``msgprint``, so ``str(exception)`` alone renders an
    empty error box.

    The queued messages are consulted only for permission errors, and only
    those queued at index ``since`` or later (the length of
    ``frappe.get_message_log()`` when the action started): the log spans the
    whole request, and an unrelated earlier message such as HRMS's "Email
    sent to ..." must not be presented as the reason for the failure
    """
    text = _clean_message(str(exception))
    if text:
        return text
    if isinstance(exception, frappe.PermissionError):
        for entry in reversed(frappe.get_message_log()[since:]):
            text = _clean_message(entry.get("message"))
            if text:
                return text
        text = _clean_message(frappe.flags.get("error_message"))
        if text:
            return text
    return type(exception).__name__

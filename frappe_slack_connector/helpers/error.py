import frappe


def generate_error_log(
    title: str,
    *,
    message: str = "",
    exception: Exception = None,
    msgprint: bool = False,
    realtime: bool = False,
):
    """
    Generate an error log with the given title and message
    """
    frappe.log_error(
        title="Slack: " + title,
        message=f"{message}\nException:\n{str(exception)}" if exception else message,
    )

    if msgprint:
        frappe.msgprint(
            title=title,
            msg=message,
            indicator="red",
            realtime=realtime,
        )

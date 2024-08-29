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
    if exception:
        message = f"{message}\nException:\n{str(exception)}"
    frappe.log_error(title=title, message=message)

    if msgprint:
        frappe.msgprint(
            title=title,
            message=message,
            indicator="red",
            realtime=realtime,
        )

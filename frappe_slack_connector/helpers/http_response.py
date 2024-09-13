import frappe
from frappe import clear_messages


def send_http_response(
    message: str | None = None,
    *,
    body: dict | None = None,
    status_code: int = 200,
    success: bool = True,
    data: dict | None = None,
    is_empty: bool = False,
) -> None:
    """
    Send an HTTP response with the given status code and data using frappe.response
    If `is_empty` is True, the response will be empty
    If `body` is provided, it will be used as the raw response
    Otherwise, return a formatted JSON response
    """
    # Clear existing messages
    clear_messages()

    # Set the status code
    frappe.response["http_status_code"] = status_code

    # Handle empty response
    if is_empty:
        frappe.response.update({})
        return

    # Handle raw body response
    if body:
        frappe.response.update(body)
        return

    # Handle formatted JSON response
    response_data = {
        "success": success and status_code >= 200 and status_code < 400,
        "message": message,
        "data": data or {},
    }

    frappe.response.update(response_data)

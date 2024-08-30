import frappe


def send_http_response(
    message: str = None,
    *,
    status_code: int = 200,
    success: bool = True,
    data: object = None,
) -> None:
    """
    Send an HTTP response with the given status code and data
    """
    frappe.response.http_status_code = status_code
    frappe.response.success = success and status_code >= 200 and status_code < 400
    frappe.response.message = message
    frappe.response.data = data or {}

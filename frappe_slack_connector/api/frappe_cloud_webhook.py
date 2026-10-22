import hmac
import json

import frappe

from frappe_slack_connector.helpers.http_response import send_http_response

# Sent by Frappe Cloud when the webhook is activated from the dashboard
VALIDATE_EVENT = "Webhook Validate"


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def receive():
    """
    API endpoint for Frappe Cloud webhooks (Frappe Cloud dashboard → Settings → Webhooks)
    Frappe Cloud waits only 5 seconds for a response, so the event is queued
    and posted to Slack by a background job.
    """
    settings = frappe.get_single("Slack Settings")
    secret = settings.get_password("frappe_cloud_webhook_secret", raise_exception=False)
    received_secret = frappe.request.headers.get("X-Webhook-Secret") or ""

    if (
        not settings.send_frappe_cloud_deploy_updates
        or not secret
        or not hmac.compare_digest(secret.encode(), received_secret.encode())
    ):
        return send_http_response("Invalid request", status_code=403)

    try:
        payload = json.loads(frappe.request.get_data(as_text=True) or "{}")
    except ValueError:
        return send_http_response("Invalid JSON payload", status_code=400)

    if not isinstance(payload, dict):
        return send_http_response("Invalid JSON payload", status_code=400)

    if payload.get("event") == VALIDATE_EVENT:
        return send_http_response("Webhook validated")

    frappe.enqueue(
        "frappe_slack_connector.tasks.frappe_cloud_deploy.process_event",
        queue="short",
        payload=payload,
    )

    return send_http_response("Event queued")

import importlib

import frappe

# The modules Slack calls into. Frappe imports a whitelisted method's module on
# the first request, so a dependency that moved underneath one of these only
# shows up as HTTP 417 on the live endpoint, with nothing in Error Log
SLACK_ENTRY_POINTS = (
    "frappe_slack_connector.api.slack_interactions",
    "frappe_slack_connector.api.slash_leave",
    "frappe_slack_connector.api.slash_timesheet",
)


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep
def slack_endpoints() -> dict:
    """
    Health check for an uptime monitor: import every Slack entry point and
    report whether they all load

    Returns ``{"ok": true}`` with HTTP 200, or ``{"ok": false, "failed": [...]}``
    with HTTP 503 naming the modules that no longer import. The exception text
    goes to Error Log, not to the anonymous caller. Python does not cache a
    failed import, so the check re-runs on every call and clears itself once a
    fix is deployed.
    """
    failures = {}
    for name in SLACK_ENTRY_POINTS:
        try:
            importlib.import_module(name)
        except Exception as e:
            failures[name] = f"{type(e).__name__}: {e}"

    if not failures:
        return {"ok": True}

    frappe.log_error(
        title="Slack: entry point import failed",
        message="\n".join(f"{name}: {error}" for name, error in failures.items()),
    )
    frappe.response["http_status_code"] = 503
    return {"ok": False, "failed": sorted(failures)}

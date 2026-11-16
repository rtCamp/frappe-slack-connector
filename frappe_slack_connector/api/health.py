import frappe

# The methods Slack calls. Frappe imports a whitelisted method's module on the
# first request, so a dependency that moved underneath one of these only shows
# up as HTTP 417 on the live endpoint, with nothing in Error Log
SLACK_ENTRY_POINTS = (
    "frappe_slack_connector.api.slack_interactions.event",
    "frappe_slack_connector.api.slash_leave.slash_leave",
    "frappe_slack_connector.api.slash_timesheet.slash_timesheet",
)
APP = "frappe_slack_connector"
# hooks.py also names assets ("frappe_slack_connector.bundle.js"), not Python
ASSET_SUFFIXES = (".js", ".css", ".html")
# One Error Log per hour while broken: the endpoint is anonymous and polled
LOG_THROTTLE_KEY = "frappe_slack_connector:health:logged"
LOG_THROTTLE_SECONDS = 3600


@frappe.whitelist(allow_guest=True, methods=["GET", "HEAD"])  # nosemgrep
def slack_endpoints() -> dict:
    """
    Health check for an uptime monitor: load every Slack entry point and
    confirm it is still a guest-whitelisted method, and load every function
    this app registers as a document or scheduler hook

    Returns ``{"ok": true}`` with HTTP 200 (Frappe wraps it as
    ``{"message": {"ok": true}}``), or HTTP 503 with ``{"ok": false,
    "failed": [...]}`` naming the entry points that no longer load. The
    exception text goes to Error Log, at most once an hour, not to the
    anonymous caller. Python does not cache a failed import, so the check
    re-runs on every call and clears itself once a fix is deployed.
    """
    failures = check_entry_points()
    if not failures:
        return {"ok": True}

    # Claim the hour atomically (SET NX EX) so overlapping probes on different
    # workers cannot each write an Error Log
    # make_key scopes the key to this site, which is what the multitenancy rule guards
    key = frappe.cache.make_key(LOG_THROTTLE_KEY)
    try:
        claimed = frappe.cache.set(  # nosemgrep: frappe-semgrep.rules.frappe-cache-breaks-multitenancy
            key,
            1,
            ex=LOG_THROTTLE_SECONDS,
            nx=True,
        )
        if claimed:
            try:
                frappe.log_error(
                    title="Slack: entry point import failed",
                    message="\n".join(f"{name}: {error}" for name, error in failures.items()),
                )
            except Exception:
                # Give the hour back so the next probe tries to log again
                frappe.cache.delete(key)
    except Exception:
        # Redis or the database being down must not turn the 503 (what the
        # monitor acts on) into a 500 without the list of what failed
        pass
    frappe.response["http_status_code"] = 503
    return {"ok": False, "failed": sorted(failures)}


def hook_entry_points() -> set[str]:
    """
    Every function of this app named anywhere in hooks.py: document and
    scheduler hooks, overrides, request hooks. An upstream rename under one
    of these breaks a save or a job, not a Slack request
    """
    paths = set()
    _collect(frappe.get_hooks(app_name=APP), paths)
    return paths


def _collect(value, paths: set[str]) -> None:
    if isinstance(value, str):
        if value.startswith(f"{APP}.") and not value.endswith(ASSET_SUFFIXES):
            paths.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, paths)
    elif isinstance(value, list | tuple | set):
        for item in value:
            _collect(item, paths)


def check_entry_points() -> dict[str, str]:
    """
    ``{dotted path: error}`` for every Slack entry point that fails to import,
    is no longer guest-whitelisted or no longer accepts POST (Slack posts),
    every hooks.py function that fails to import, and the optional next_pms
    helper when next_pms is installed but the import failed
    """
    failures = {}
    for path in SLACK_ENTRY_POINTS:
        try:
            method = frappe.get_attr(path)
        except Exception as e:
            failures[path] = f"{type(e).__name__}: {e}"
            continue
        if method not in frappe.guest_methods:
            failures[path] = "not whitelisted for guests"
        elif "POST" not in frappe.allowed_http_methods_for_whitelisted_func.get(method, []):
            failures[path] = "POST not allowed"
    for path in sorted(hook_entry_points() - set(SLACK_ENTRY_POINTS)):
        try:
            frappe.get_attr(path)
        except Exception as e:
            failures[path] = f"{type(e).__name__}: {e}"
    failures.update(optional_dependency_failures())
    # frappe.get_attr reports some failures with frappe.throw, which also
    # queues the text for the client; keep it out of the anonymous response
    frappe.clear_messages()
    return failures


def optional_dependency_failures() -> dict[str, str]:
    """
    The workload reminder imports its next_pms helper under try/except so
    the app installs without next_pms. On a site that has next_pms, a
    failed import is an upstream rename of exactly the kind this check
    exists for, and would otherwise only surface as an Error Log when the
    reminder runs
    """
    from frappe_slack_connector.db.timesheet import is_next_pms_installed
    from frappe_slack_connector.tasks import workload_reminder

    if is_next_pms_installed() and not workload_reminder.IMPORT_SUCCESS:
        return {f"{APP}.tasks.workload_reminder": "next_pms is installed but its allocation helper no longer imports"}
    return {}

import json

import frappe
from frappe.utils import add_to_date, now_datetime

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration

####################################################################
#                                                                  #
# Frappe Cloud bench updates                                       #
# -----------------------------------------------------------------#
# Posts one Slack thread per Frappe Cloud deploy with only the     #
# milestones: update started, update complete, and failures.       #
#                                                                  #
####################################################################

THREAD_DOCTYPE = "Frappe Cloud Deploy Thread"

DEPLOY_EVENT = "Bench Deploy Status Update"
BENCH_EVENT = "Bench Status Update"
SITE_EVENT = "Site Status Update"

# Draft and Scheduled deploys have not started building yet
STARTED_DEPLOY_STATUSES = {"Pending", "Preparing", "Running", "Success", "Failure"}

# Bench and site events carry no reference to the deploy, so they go to the
# latest deploy thread of their bench group if it was started within this window
THREAD_WINDOW_HOURS = 48

BUILD_ERROR_CHAR_LIMIT = 2500
DASHBOARD_DEPLOY_URL = "https://frappecloud.com/dashboard/groups/{group}/deploys/{deploy}"

PARENT_TEXT = "Bench Update :thread:"


def process_event(payload: dict) -> None:
    """
    Background job for a Frappe Cloud webhook event
    """
    event, data = _unwrap_payload(payload)
    if not event or not data:
        return

    settings = frappe.get_single("Slack Settings")
    if not settings.send_frappe_cloud_deploy_updates:
        return

    watch = get_watch(settings, data.get("group"))
    if not watch:
        return

    try:
        if event == DEPLOY_EVENT:
            post_deploy_update(data, watch.channel_id)
        elif event == BENCH_EVENT:
            post_bench_update(data, watch.sites)
        elif event == SITE_EVENT and data.get("name") in watch.sites:
            post_site_update(data, watch.sites)
    except Exception as e:
        frappe.db.rollback()
        generate_error_log(
            title="Error posting Frappe Cloud deploy update",
            message=json.dumps(payload, indent=2, default=str),
            exception=e,
        )


def _unwrap_payload(payload: dict) -> tuple[str | None, dict]:
    """
    Return the event name and its data. Events are sent as {"event": ..., "data": ...},
    but the Frappe Cloud docs show Site Status Update as the bare Site document.
    """
    if payload.get("event"):
        return payload["event"], payload.get("data") or {}
    if payload.get("doctype") == "Site":
        return SITE_EVENT, payload
    return None, {}


def get_watch(settings, group: str | None) -> frappe._dict | None:
    """
    Return the channel and sites to wait for on the bench group, or None when
    the bench group is not watched. An empty table watches every bench group.
    """
    if not group:
        return None

    if not settings.frappe_cloud_bench_groups:
        return frappe._dict(channel_id=settings.frappe_cloud_channel_id, sites=set())

    for row in settings.frappe_cloud_bench_groups:
        if row.bench_group.strip() == group:
            return frappe._dict(
                channel_id=row.channel_id or settings.frappe_cloud_channel_id,
                sites={site.strip() for site in (row.sites or "").splitlines() if site.strip()},
            )
    return None


def post_deploy_update(data: dict, channel_id: str) -> None:
    """
    Start the thread once the build begins, and report a failed build
    """
    deploy, group, status = data.get("name"), data.get("group"), data.get("status")
    candidate = data.get("deploy_candidate")
    if not deploy or status not in STARTED_DEPLOY_STATUSES:
        return

    if not frappe.db.exists(THREAD_DOCTYPE, deploy) and is_superseded(group, candidate):
        # A retried delivery of an older deploy arriving after a newer one has started
        return

    thread = get_or_create_thread(deploy, group, channel_id, candidate)
    thread.status = status
    slack = SlackIntegration()

    if not thread.thread_ts:
        response = slack.slack_app.client.chat_postMessage(channel=thread.channel_id, text=PARENT_TEXT)
        thread.thread_ts = response["ts"]
        thread.channel_id = response.get("channel") or thread.channel_id

    if status != "Failure" and mark_posted(thread, "started"):
        url = DASHBOARD_DEPLOY_URL.format(group=group, deploy=deploy)
        reply(slack, thread, f"Bench update started on `{group}` (<{url}|view deploy>)")

    if status == "Failure" and mark_posted(thread, "failed"):
        thread.build_error = _clean(data.get("build_error"))
        text = f":x: Bench update failed: the build for `{group}` failed"
        if thread.build_error:
            text += f"\n```{thread.build_error[-BUILD_ERROR_CHAR_LIMIT:]}```"
        reply(slack, thread, text)

    thread.save(ignore_permissions=True)


def post_bench_update(data: dict, sites: set[str]) -> None:
    """
    Report a broken bench, and mark the update complete once the new bench is
    live when there are no sites to wait for
    """
    bench, status = data.get("name"), data.get("status")
    thread = get_thread_for_bench(data.get("group"), bench)
    if not thread:
        return

    if status == "Broken" and mark_posted(thread, f"broken:{bench}"):
        thread.status = "Failure"
        reply(SlackIntegration(), thread, f":x: Bench update failed: the new bench `{bench}` is broken")
        thread.save(ignore_permissions=True)
    elif status == "Active" and mark_posted(thread, "bench:Active"):
        complete_if_done(thread, sites)
        thread.save(ignore_permissions=True)


def post_site_update(data: dict, sites: set[str]) -> None:
    """
    Record a watched site coming back online on the deploy's new bench
    """
    site, status, group = data.get("name"), data.get("status"), data.get("group")

    if status == "Broken":
        # A site whose update failed may still be on its old bench, so report it in the latest thread
        thread = get_latest_thread(group)
        if thread and mark_posted(thread, f"broken:{site}"):
            thread.status = "Failure"
            reply(SlackIntegration(), thread, f":x: Bench update failed: site `{site}` is broken")
            thread.save(ignore_permissions=True)
        return

    thread = get_thread_for_bench(group, data.get("bench"))
    if thread and status == "Active" and "bench:Active" in _posted(thread) and mark_posted(thread, f"site:{site}"):
        complete_if_done(thread, sites)
        thread.save(ignore_permissions=True)


def complete_if_done(thread, sites: set[str]) -> None:
    """
    Post the completion message once the new bench is live and every watched site is back online
    """
    posted = _posted(thread)
    if "bench:Active" not in posted or any(f"site:{site}" not in posted for site in sites):
        return
    if not mark_posted(thread, "complete"):
        return

    thread.status = "Complete"
    if sites:
        text = ":white_check_mark: Bench update complete, all sites are back online"
    else:
        text = ":white_check_mark: Bench update complete, the new bench is live"
    reply(SlackIntegration(), thread, text)


def get_or_create_thread(deploy: str, group: str | None, channel_id: str, candidate: str | None):
    """
    Return the thread for the deploy, locked until the job commits so that
    concurrent events of the same deploy do not post two parent messages
    """
    if not frappe.db.exists(THREAD_DOCTYPE, deploy):
        try:
            frappe.get_doc(
                {
                    "doctype": THREAD_DOCTYPE,
                    "deploy": deploy,
                    "deploy_candidate": candidate,
                    "bench_group": group,
                    "channel_id": channel_id,
                }
            ).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            # Created by a concurrent job for the same deploy
            pass
    return frappe.get_doc(THREAD_DOCTYPE, deploy, for_update=True)


def _recent_threads(group: str | None) -> list[frappe._dict]:
    """
    Deploy threads of the bench group started within the window, newest first
    """
    if not group:
        return []
    return frappe.get_all(
        THREAD_DOCTYPE,
        filters={
            "bench_group": group,
            "thread_ts": ("is", "set"),
            "creation": (">=", add_to_date(now_datetime(), hours=-THREAD_WINDOW_HOURS)),
        },
        fields=["name", "deploy_candidate"],
        order_by="creation desc",
    )


def get_latest_thread(group: str | None):
    """
    Return the latest deploy thread of the bench group started within the window
    """
    threads = _recent_threads(group)
    return frappe.get_doc(THREAD_DOCTYPE, threads[0].name, for_update=True) if threads else None


def get_thread_for_bench(group: str | None, bench: str | None):
    """
    Return the deploy thread that created the bench. Frappe Cloud names a bench
    after its deploy candidate, so deploy-49638-000005 builds bench-49638-000005-<server>.
    """
    if not bench:
        return None
    for thread in _recent_threads(group):
        if thread.deploy_candidate and bench.startswith(f"bench-{thread.deploy_candidate.removeprefix('deploy-')}-"):
            return frappe.get_doc(THREAD_DOCTYPE, thread.name, for_update=True)
    return None


def is_superseded(group: str | None, candidate: str | None) -> bool:
    """
    Whether a newer deploy candidate of the bench group already has a thread
    """
    number = _candidate_number(candidate)
    if number is None:
        return False
    return any((_candidate_number(t.deploy_candidate) or 0) > number for t in _recent_threads(group))


def _candidate_number(candidate: str | None) -> int | None:
    """
    The sequence number of a deploy candidate, 5 for deploy-49638-000005
    """
    suffix = (candidate or "").rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else None


def mark_posted(thread, key: str) -> bool:
    """
    Record the milestone on the thread. Returns False if it was already recorded,
    since Frappe Cloud retries deliveries and can send a bench status twice.
    """
    posted = _posted(thread)
    if key in posted:
        return False
    thread.posted_events = "\n".join([*posted, key])
    return True


def _posted(thread) -> list[str]:
    return (thread.posted_events or "").splitlines()


def reply(slack: SlackIntegration, thread, text: str) -> None:
    slack.slack_app.client.chat_postMessage(channel=thread.channel_id, thread_ts=thread.thread_ts, text=text)


def _clean(value) -> str:
    """
    Frappe Cloud serialises empty fields as "None"
    """
    if value is None or str(value) == "None":
        return ""
    return str(value)

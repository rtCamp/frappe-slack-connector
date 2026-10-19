import json

import frappe
from frappe.utils import add_to_date, now_datetime

from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration

####################################################################
#                                                                  #
# Frappe Cloud deploy updates                                      #
# -----------------------------------------------------------------#
# Posts one Slack message per Frappe Cloud deploy, and replies in  #
# its thread for every later status change of the deploy, its new  #
# benches and the sites moved onto them.                           #
#                                                                  #
####################################################################

THREAD_DOCTYPE = "Frappe Cloud Deploy Thread"

DEPLOY_EVENT = "Bench Deploy Status Update"
BENCH_EVENT = "Bench Status Update"
SITE_EVENT = "Site Status Update"

# Bench and site statuses worth posting; the rest are noise for a deploy thread
BENCH_STATUSES = {"Installing", "Updating", "Active", "Broken"}
SITE_STATUSES = {"Updating", "Active", "Broken", "Recovering"}

# Bench and site events carry no reference to the deploy, so they go to the
# latest deploy thread of their bench group if it was started within this window
THREAD_WINDOW_HOURS = 48

BUILD_ERROR_CHAR_LIMIT = 2500
DASHBOARD_DEPLOY_URL = "https://frappecloud.com/dashboard/groups/{group}/deploys/{deploy}"

STATUS_EMOJI = {
    "Success": ":white_check_mark:",
    "Active": ":white_check_mark:",
    "Failure": ":x:",
    "Broken": ":x:",
    "Preparing": ":gear:",
    "Running": ":gear:",
    "Installing": ":gear:",
    "Updating": ":gear:",
    "Recovering": ":gear:",
}
DEFAULT_EMOJI = ":hourglass_flowing_sand:"


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
            post_bench_update(data)
        elif event == SITE_EVENT and (not watch.sites or data.get("name") in watch.sites):
            post_site_update(data)
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
    Return the channel and sites to report for the bench group, or None when
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
    Post the parent message for a new deploy, or update it and reply in its thread
    """
    deploy, group, status = data.get("name"), data.get("group"), data.get("status")
    if not deploy or not status:
        return

    thread = get_or_create_thread(deploy, group, channel_id)
    if not mark_posted(thread, f"deploy:{status}"):
        return

    thread.status = status
    build_error = _clean(data.get("build_error"))
    if build_error:
        thread.build_error = build_error

    slack = SlackIntegration()
    parent_text = get_parent_text(thread)
    if thread.thread_ts:
        slack.slack_app.client.chat_update(channel=thread.channel_id, ts=thread.thread_ts, text=parent_text)
    else:
        response = slack.slack_app.client.chat_postMessage(channel=thread.channel_id, text=parent_text)
        thread.thread_ts = response["ts"]
        # The setting may hold a channel name, but chat.update only accepts the channel ID
        thread.channel_id = response.get("channel") or thread.channel_id

    reply(slack, thread, get_deploy_reply_text(data))
    thread.save(ignore_permissions=True)


def post_bench_update(data: dict) -> None:
    """
    Reply in the latest deploy thread when one of its benches changes status
    """
    bench, status = data.get("name"), data.get("status")
    if not bench or status not in BENCH_STATUSES:
        return

    thread = get_latest_thread(data.get("group"))
    if not thread or not mark_posted(thread, f"bench:{bench}:{status}"):
        return

    cluster = f" ({data['cluster']})" if data.get("cluster") else ""
    reply(SlackIntegration(), thread, f"{get_emoji(status)} Bench `{bench}`{cluster} is *{status}*")
    thread.save(ignore_permissions=True)


def post_site_update(data: dict) -> None:
    """
    Reply in the latest deploy thread when a site on the bench group changes status
    """
    site, status = data.get("name"), data.get("status")
    if not site or status not in SITE_STATUSES:
        return

    thread = get_latest_thread(data.get("group"))
    if not thread or not mark_posted(thread, f"site:{site}:{status}"):
        return

    reply(SlackIntegration(), thread, f"{get_emoji(status)} Site `{site}` is *{status}*")
    thread.save(ignore_permissions=True)


def get_or_create_thread(deploy: str, group: str | None, channel_id: str):
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
                    "bench_group": group,
                    "channel_id": channel_id,
                }
            ).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            # Created by a concurrent job for the same deploy
            pass
    return frappe.get_doc(THREAD_DOCTYPE, deploy, for_update=True)


def get_latest_thread(group: str | None):
    """
    Return the latest deploy thread of the bench group started within the window
    """
    if not group:
        return None
    name = frappe.db.get_value(
        THREAD_DOCTYPE,
        {
            "bench_group": group,
            "thread_ts": ("is", "set"),
            "creation": (">=", add_to_date(now_datetime(), hours=-THREAD_WINDOW_HOURS)),
        },
        "name",
        order_by="creation desc",
    )
    return frappe.get_doc(THREAD_DOCTYPE, name, for_update=True) if name else None


def mark_posted(thread, key: str) -> bool:
    """
    Record the event on the thread. Returns False if it was already posted,
    since Frappe Cloud retries deliveries and can send a bench status twice.
    """
    posted = (thread.posted_events or "").splitlines()
    if key in posted:
        return False
    thread.posted_events = "\n".join([*posted, key])
    return True


def reply(slack: SlackIntegration, thread, text: str) -> None:
    slack.slack_app.client.chat_postMessage(channel=thread.channel_id, thread_ts=thread.thread_ts, text=text)


def get_parent_text(thread) -> str:
    url = DASHBOARD_DEPLOY_URL.format(group=thread.bench_group, deploy=thread.deploy)
    return (
        f"{get_emoji(thread.status)} *Frappe Cloud deploy* `{thread.deploy}` on `{thread.bench_group}`: "
        f"*{thread.status}*\n<{url}|View in Frappe Cloud>"
    )


def get_deploy_reply_text(data: dict) -> str:
    status = data.get("status")
    text = f"{get_emoji(status)} Build is *{status}*"

    build_duration = _clean(data.get("build_duration"))
    if status == "Success" and build_duration:
        # Drop the microseconds from "0:00:41.794537"
        text += f" in {build_duration.split('.')[0]}"

    build_error = _clean(data.get("build_error"))
    if status == "Failure" and build_error:
        text += f"\n```{build_error[-BUILD_ERROR_CHAR_LIMIT:]}```"
    return text


def get_emoji(status: str | None) -> str:
    return STATUS_EMOJI.get(status, DEFAULT_EMOJI)


def _clean(value) -> str:
    """
    Frappe Cloud serialises empty fields as "None"
    """
    if value is None or str(value) == "None":
        return ""
    return str(value)

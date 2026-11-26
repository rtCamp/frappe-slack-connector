"""The mirror's own Slack client, the checks on a target channel, and which Slack errors a retry cannot fix."""

import frappe
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# Times one call waits out an HTTP 429 (for the Retry-After Slack sends) before giving up
RATE_LIMIT_RETRIES = 3

# Why a channel cannot take a mirrored thread; "{0}" is the channel name
PROBLEM_MESSAGES = {
    "channel_not_found": "Slack channel #{0} no longer exists or is not visible to the bot.",
    "is_archived": "Slack channel #{0} is archived.",
    "ext_shared": "Slack channel #{0} is shared with another organisation. Email threads cannot be mirrored there.",
    "not_in_channel": "Invite the Slack bot to #{0} first.",
}

# Slack errors that will not go away by trying again: the mirror fails at once
FATAL_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "thread_not_found",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "missing_scope",
        "no_permission",
        "restricted_action",
    }
)


def mirror_client() -> WebClient:
    """
    A Slack client for the mirror job. It waits out rate limits, which is safe
    because Slack has not posted the message it refused. It is given no other
    retry handler: the SDK's default re-sends a call after a dropped connection,
    and Slack may already have accepted that post.
    """
    token = frappe.get_single("Slack Settings").get_password("slack_bot_token")
    return WebClient(token=token, retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES)])


def slack_error_code(exc: Exception) -> str | None:
    """Slack's error code for a failed API call, None for any other exception."""
    if isinstance(exc, SlackApiError):
        return exc.response.get("error")
    return None


def channel_problem(client, channel_id: str) -> str | None:
    """
    Why the channel cannot take a mirrored thread, as a key of PROBLEM_MESSAGES,
    or None when it can. A channel shared with another organisation is refused
    whether or not the bot is in it: email text must not leave the workspace.
    """
    try:
        channel = client.conversations_info(channel=channel_id)["channel"]
    except SlackApiError as exc:
        if slack_error_code(exc) == "channel_not_found":
            return "channel_not_found"
        raise
    if channel.get("is_archived"):
        return "is_archived"
    if channel.get("is_ext_shared") or channel.get("is_pending_ext_shared"):
        return "ext_shared"
    if not channel.get("is_member"):
        return "not_in_channel"
    return None

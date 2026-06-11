"""Tests setup and helpers."""

import contextlib
import hashlib
import hmac
import time
from unittest.mock import MagicMock

import frappe
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

TEST_USER = "test_fsc_user1@example.com"
TEST_USER_2 = "test_fsc_user2@example.com"
TEST_USER_3 = "test_fsc_user3@example.com"

TEST_SLACK_USER_ID = "U0FSC0001"
TEST_SLACK_USER_ID_2 = "U0FSC0002"
TEST_SLACK_USERNAME = "fsc_user1"
TEST_SLACK_CHANNEL_ID = "C0FSC0001"
TEST_SLACK_WORKLOAD_CHANNEL_ID = "C0FSC0002"

TEST_SIGNING_SECRET = "test-signing-secret"


def ensure_doc(doctype, name, **fields):
    """Return the existing doc with this name, or insert one and return it. Caller is responsible for picking a doctype whose autoname respects the supplied name."""
    if frappe.db.exists(doctype, name):
        return frappe.get_doc(doctype, name)
    return frappe.get_doc({"doctype": doctype, "name": name, **fields}).insert(ignore_permissions=True)


def set_password_field(doctype, name, fieldname, value):
    """Set or clear a Password field via the encrypted-storage helpers.

    Frappe stores Password fields in two places: the encrypted `__Auth` table
    (read by `doc.get_password`) and the doctype column (read by attribute
    access `doc.<fieldname>`, which source code uses for truthy/falsy gates).
    This helper writes both so the field stays consistent. Passing an empty
    string or None clears both sides.
    """
    if value:
        set_encrypted_password(doctype, name, value, fieldname)
    else:
        remove_encrypted_password(doctype, name, fieldname)
    frappe.db.set_value(doctype, name, fieldname, value or "", update_modified=False)


def make_test_user(email, user_type="System User"):
    """Create a User with no welcome email. user_type controls Website User vs System User (the latter is what most FSC tests need).

    Frappe's User.validate auto-downgrades System Users with no system roles to Website Users; we force the requested user_type via db.set_value after insert so callers see what they asked for.
    """
    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@")[0],
                "send_welcome_email": 0,
                "user_type": user_type,
            }
        ).insert(ignore_permissions=True)
    frappe.db.set_value("User", email, "user_type", user_type)
    return frappe.get_doc("User", email)


def make_test_user_meta(user, slack_userid=TEST_SLACK_USER_ID, slack_username=TEST_SLACK_USERNAME):
    """Insert or update a User Meta row mapping the supplied Frappe user to a Slack user_id and username."""
    if frappe.db.exists("User Meta", {"user": user}):
        name = frappe.db.get_value("User Meta", {"user": user})
        frappe.db.set_value(
            "User Meta",
            name,
            {
                "custom_slack_userid": slack_userid,
                "custom_slack_username": slack_username,
            },
        )
        return frappe.get_doc("User Meta", name)
    doc = frappe.get_doc(
        {
            "doctype": "User Meta",
            "user": user,
            "custom_slack_userid": slack_userid,
            "custom_slack_username": slack_username,
        }
    )
    doc.flags.ignore_validate = True
    doc.insert(ignore_permissions=True)
    return doc


def make_test_slack_channel(channel_id=TEST_SLACK_CHANNEL_ID, channel_name="test-fsc"):
    """Insert or return a Slack Channel row keyed by channel_id."""
    existing = frappe.db.get_value("Slack Channel", {"channel_id": channel_id})
    if existing:
        return frappe.get_doc("Slack Channel", existing)
    doc = frappe.get_doc(
        {
            "doctype": "Slack Channel",
            "channel_id": channel_id,
            "channel_name": channel_name,
        }
    )
    doc.flags.ignore_validate = True
    doc.insert(ignore_permissions=True)
    return doc


@contextlib.contextmanager
def as_user(email):
    """Temporarily switch frappe.session.user for the duration of the block."""
    original = frappe.session.user
    frappe.set_user(email)
    try:
        yield
    finally:
        frappe.set_user(original)


def build_slack_client_mock(**overrides):
    """Return a MagicMock that mimics the Slack Bolt SDK client surface.

    Configures the methods the FSC source code reaches for: chat.postMessage,
    chat.update, views.open, views.update, views.push, users.lookupByEmail,
    users.list, conversations.list. Default returns are empty/successful;
    pass overrides as keyword args (e.g. users_lookupByEmail={"user": {...}})
    to set per-method return values.
    """
    client = MagicMock()
    defaults = {
        "chat_postMessage": {"ok": True, "ts": "1700000000.000001"},
        "chat_update": {"ok": True, "ts": "1700000000.000001"},
        "views_open": {"ok": True},
        "views_update": {"ok": True},
        "views_push": {"ok": True},
        "users_lookupByEmail": {
            "ok": True,
            "user": {"id": TEST_SLACK_USER_ID, "name": TEST_SLACK_USERNAME},
        },
        "users_list": {"ok": True, "members": []},
        "conversations_list": {"ok": True, "channels": []},
    }
    defaults.update(overrides)
    for method_name, return_value in defaults.items():
        getattr(client, method_name).return_value = return_value
    return client


def build_signed_slack_request(body: str, signing_secret: str = TEST_SIGNING_SECRET, timestamp: int | None = None):
    """Build the (body, headers) pair for a Slack-signed request.

    Returns the raw body and a headers dict with X-Slack-Signature and
    X-Slack-Request-Timestamp computed per Slack's v0 HMAC-SHA256 scheme.
    Defaults to the current timestamp; pass an older value to test
    replay-protection rejection.
    """
    if timestamp is None:
        timestamp = int(time.time())
    base = f"v0:{timestamp}:{body}".encode()
    signature = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return body, {
        "X-Slack-Signature": signature,
        "X-Slack-Request-Timestamp": str(timestamp),
    }

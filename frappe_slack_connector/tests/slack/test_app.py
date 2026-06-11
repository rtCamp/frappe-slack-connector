import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.app import SlackIntegration
from frappe_slack_connector.tests import (
    TEST_SIGNING_SECRET,
    TEST_SLACK_CHANNEL_ID,
    TEST_SLACK_USER_ID,
    TEST_SLACK_USERNAME,
    TEST_USER,
    build_signed_slack_request,
    build_slack_client_mock,
    make_test_user,
    make_test_user_meta,
    set_password_field,
)

SLACK_APP_MODULE = "frappe_slack_connector.slack.app"


def _seed_slack_settings():
    """Populate Slack Settings with sentinel values so SlackIntegration init succeeds."""
    set_password_field("Slack Settings", "Slack Settings", "slack_bot_token", "xoxb-test")
    set_password_field("Slack Settings", "Slack Settings", "slack_app_token", "xapp-test")
    set_password_field("Slack Settings", "Slack Settings", "slack_signing_token", TEST_SIGNING_SECRET)
    set_password_field(
        "Slack Settings",
        "Slack Settings",
        "attendance_channel_id",
        TEST_SLACK_CHANNEL_ID,
    )


def _build_integration(client_mock):
    """Build a SlackIntegration whose slack_app.client is the supplied mock. Slack Settings must be pre-populated."""
    with patch(f"{SLACK_APP_MODULE}.App") as MockApp:
        MockApp.return_value.client = client_mock
        return SlackIntegration()


class TestCheckSlackConfig(IntegrationTestCase):
    def _bare(self, **slack_attrs):
        """Build a SlackIntegration instance with only the SLACK_ attrs set (bypassing __init__)."""
        instance = SlackIntegration.__new__(SlackIntegration)
        for k, v in slack_attrs.items():
            setattr(instance, k, v)
        return instance

    def test_returns_true_when_all_slack_attributes_are_set(self):
        """__check_slack_config returns True when every SLACK_ attribute is non-None."""
        slack = self._bare(
            SLACK_BOT_TOKEN="b",
            SLACK_APP_TOKEN="a",
            SLACK_CHANNEL_ID="c",
            SLACK_SIGNATURE="s",
        )
        self.assertTrue(slack._SlackIntegration__check_slack_config())

    def test_returns_false_when_bot_token_missing(self):
        """__check_slack_config returns False when SLACK_BOT_TOKEN is None."""
        slack = self._bare(
            SLACK_BOT_TOKEN=None,
            SLACK_APP_TOKEN="a",
            SLACK_CHANNEL_ID="c",
            SLACK_SIGNATURE="s",
        )
        self.assertFalse(slack._SlackIntegration__check_slack_config())

    def test_returns_false_when_app_token_missing(self):
        """__check_slack_config returns False when SLACK_APP_TOKEN is None."""
        slack = self._bare(
            SLACK_BOT_TOKEN="b",
            SLACK_APP_TOKEN=None,
            SLACK_CHANNEL_ID="c",
            SLACK_SIGNATURE="s",
        )
        self.assertFalse(slack._SlackIntegration__check_slack_config())

    def test_returns_false_when_channel_id_missing(self):
        """__check_slack_config returns False when SLACK_CHANNEL_ID is None."""
        slack = self._bare(
            SLACK_BOT_TOKEN="b",
            SLACK_APP_TOKEN="a",
            SLACK_CHANNEL_ID=None,
            SLACK_SIGNATURE="s",
        )
        self.assertFalse(slack._SlackIntegration__check_slack_config())

    def test_returns_false_when_signing_token_missing(self):
        """__check_slack_config returns False when SLACK_SIGNATURE is None."""
        slack = self._bare(
            SLACK_BOT_TOKEN="b",
            SLACK_APP_TOKEN="a",
            SLACK_CHANNEL_ID="c",
            SLACK_SIGNATURE=None,
        )
        self.assertFalse(slack._SlackIntegration__check_slack_config())


class TestGetSlackUsers(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _seed_slack_settings()

    def _user_row(
        self,
        user_id,
        email,
        *,
        deleted=False,
        is_bot=False,
        is_app_user=False,
        name="user",
        real_name="User",
    ):
        return {
            "id": user_id,
            "name": name,
            "real_name": real_name,
            "deleted": deleted,
            "is_bot": is_bot,
            "is_app_user": is_app_user,
            "profile": {"email": email},
        }

    def test_filters_deleted_bot_and_app_users(self):
        """get_slack_users excludes users with deleted=True, is_bot=True, or is_app_user=True from the returned dict."""
        client = build_slack_client_mock(
            users_list={
                "ok": True,
                "members": [
                    self._user_row("U1", "deleted@x.com", deleted=True),
                    self._user_row("U2", "bot@x.com", is_bot=True),
                    self._user_row("U3", "app@x.com", is_app_user=True),
                    self._user_row("U4", "active@x.com"),
                ],
                "response_metadata": {"next_cursor": ""},
            }
        )
        slack = _build_integration(client)
        result = slack.get_slack_users()
        self.assertEqual(set(result.keys()), {"active@x.com"})

    def test_filters_users_without_email(self):
        """get_slack_users excludes users whose profile has no email."""
        client = build_slack_client_mock(
            users_list={
                "ok": True,
                "members": [
                    {
                        "id": "U1",
                        "name": "noemail",
                        "real_name": "No Email",
                        "deleted": False,
                        "is_bot": False,
                        "is_app_user": False,
                        "profile": {},
                    },
                    self._user_row("U2", "with@x.com"),
                ],
                "response_metadata": {"next_cursor": ""},
            }
        )
        slack = _build_integration(client)
        result = slack.get_slack_users()
        self.assertEqual(set(result.keys()), {"with@x.com"})

    def test_paginates_via_next_cursor_until_empty(self):
        """get_slack_users follows response_metadata.next_cursor until empty."""
        client = build_slack_client_mock()
        client.users_list.side_effect = [
            {
                "ok": True,
                "members": [self._user_row("U1", "p1@x.com")],
                "response_metadata": {"next_cursor": "abc"},
            },
            {
                "ok": True,
                "members": [self._user_row("U2", "p2@x.com")],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        slack = _build_integration(client)
        result = slack.get_slack_users()
        self.assertEqual(set(result.keys()), {"p1@x.com", "p2@x.com"})
        self.assertEqual(client.users_list.call_count, 2)
        # Second call must include the cursor returned by the first.
        self.assertEqual(client.users_list.call_args_list[1].kwargs["cursor"], "abc")

    def test_returns_mapping_with_id_name_and_real_name(self):
        """Each returned row contains the user's id, name, and real_name."""
        client = build_slack_client_mock(
            users_list={
                "ok": True,
                "members": [self._user_row("U7", "x@x.com", name="x", real_name="Ex")],
                "response_metadata": {"next_cursor": ""},
            }
        )
        slack = _build_integration(client)
        result = slack.get_slack_users()
        self.assertEqual(result["x@x.com"], {"id": "U7", "name": "x", "real_name": "Ex"})


class TestGetSlackUser(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _seed_slack_settings()
        make_test_user(TEST_USER)

    def test_returns_cached_value_without_api_call_when_check_meta(self):
        """get_slack_user(check_meta=True) returns the slack identifiers from User Meta without calling the Slack API."""
        make_test_user_meta(TEST_USER)
        client = build_slack_client_mock()
        slack = _build_integration(client)
        result = slack.get_slack_user(user_email=TEST_USER, check_meta=True)
        self.assertEqual(result, {"id": TEST_SLACK_USER_ID, "name": TEST_SLACK_USERNAME})
        client.users_lookupByEmail.assert_not_called()

    def test_calls_api_when_check_meta_false_and_from_api_true(self):
        """get_slack_user(check_meta=False, from_api=True) calls users_lookupByEmail directly."""
        client = build_slack_client_mock()
        slack = _build_integration(client)
        slack.get_slack_user(user_email="other@x.com", check_meta=False, from_api=True)
        client.users_lookupByEmail.assert_called_once_with(email="other@x.com")

    def test_returns_none_when_no_meta_and_from_api_false(self):
        """get_slack_user(check_meta=True, from_api=False) returns None when no User Meta exists."""
        # No make_test_user_meta call; ensure no row exists for this user.
        client = build_slack_client_mock()
        slack = _build_integration(client)
        result = slack.get_slack_user(user_email="nobody@x.com", check_meta=True, from_api=False)
        self.assertIsNone(result)
        client.users_lookupByEmail.assert_not_called()

    def test_raises_value_error_when_neither_email_nor_employee_id_supplied(self):
        """get_slack_user raises ValueError when both user_email and employee_id are None."""
        client = build_slack_client_mock()
        slack = _build_integration(client)
        with self.assertRaises(ValueError):
            slack.get_slack_user()

    def test_raises_value_error_when_both_email_and_employee_id_supplied(self):
        """get_slack_user raises ValueError when both user_email and employee_id are passed."""
        client = build_slack_client_mock()
        slack = _build_integration(client)
        with self.assertRaises(ValueError):
            slack.get_slack_user(user_email="a@x.com", employee_id="EMP-001")


class TestGetSlackUserId(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _seed_slack_settings()
        make_test_user(TEST_USER)
        make_test_user_meta(TEST_USER)

    def test_returns_just_the_id_field_from_get_slack_user(self):
        """get_slack_user_id returns only the 'id' value from the get_slack_user result."""
        client = build_slack_client_mock()
        slack = _build_integration(client)
        result = slack.get_slack_user_id(user_email=TEST_USER, check_meta=True)
        self.assertEqual(result, TEST_SLACK_USER_ID)

    def test_returns_none_when_get_slack_user_returns_none(self):
        """get_slack_user_id returns None when get_slack_user returns None."""
        client = build_slack_client_mock()
        slack = _build_integration(client)
        result = slack.get_slack_user_id(user_email="nobody@x.com", check_meta=True, from_api=False)
        self.assertIsNone(result)


class TestGetSlackChannels(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _seed_slack_settings()

    def test_requests_public_and_private_channels_excluding_archived(self):
        """get_slack_channels passes types='public_channel,private_channel' and exclude_archived=True to conversations.list."""
        client = build_slack_client_mock(
            conversations_list={
                "ok": True,
                "channels": [],
                "response_metadata": {"next_cursor": ""},
            }
        )
        slack = _build_integration(client)
        slack.get_slack_channels()
        kwargs = client.conversations_list.call_args.kwargs
        self.assertEqual(kwargs["types"], "public_channel,private_channel")
        self.assertTrue(kwargs["exclude_archived"])

    def test_paginates_via_next_cursor_until_empty(self):
        """get_slack_channels follows response_metadata.next_cursor until empty."""
        client = build_slack_client_mock()
        client.conversations_list.side_effect = [
            {
                "ok": True,
                "channels": [{"id": "C1", "name": "one"}],
                "response_metadata": {"next_cursor": "xyz"},
            },
            {
                "ok": True,
                "channels": [{"id": "C2", "name": "two"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        slack = _build_integration(client)
        result = slack.get_slack_channels()
        self.assertEqual(result, [{"id": "C1", "name": "one"}, {"id": "C2", "name": "two"}])
        self.assertEqual(client.conversations_list.call_count, 2)
        self.assertEqual(client.conversations_list.call_args_list[1].kwargs["cursor"], "xyz")

    def test_returns_channels_as_id_name_dicts(self):
        """Each returned channel dict has just id and name fields."""
        client = build_slack_client_mock(
            conversations_list={
                "ok": True,
                "channels": [
                    {"id": "C9", "name": "real", "is_archived": False, "extra": "junk"},
                ],
                "response_metadata": {"next_cursor": ""},
            }
        )
        slack = _build_integration(client)
        result = slack.get_slack_channels()
        self.assertEqual(result, [{"id": "C9", "name": "real"}])


class TestVerifySlackRequest(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _seed_slack_settings()

    def test_returns_silently_when_signature_and_timestamp_are_valid(self):
        """verify_slack_request returns None without raising when the supplied signature matches and the timestamp is fresh."""
        body, headers = build_signed_slack_request("payload=ok")
        slack = _build_integration(build_slack_client_mock())
        # Returns None on success.
        self.assertIsNone(
            slack.verify_slack_request(
                signature=headers["X-Slack-Signature"],
                timestamp=headers["X-Slack-Request-Timestamp"],
                req_data=body,
            )
        )

    def test_raises_permission_error_on_signature_mismatch(self):
        """verify_slack_request raises frappe.PermissionError when the supplied signature does not match the computed HMAC."""
        body, headers = build_signed_slack_request("payload=ok")
        slack = _build_integration(build_slack_client_mock())
        with self.assertRaises(frappe.PermissionError):
            slack.verify_slack_request(
                signature="v0=wrong",
                timestamp=headers["X-Slack-Request-Timestamp"],
                req_data=body,
            )

    def test_raises_permission_error_when_timestamp_older_than_five_minutes(self):
        """verify_slack_request raises frappe.PermissionError when the timestamp is more than 300 seconds old (replay protection)."""
        old_ts = int(time.time()) - (6 * 60)
        body, headers = build_signed_slack_request("payload=old", timestamp=old_ts)
        slack = _build_integration(build_slack_client_mock())
        with self.assertRaises(frappe.PermissionError):
            slack.verify_slack_request(
                signature=headers["X-Slack-Signature"],
                timestamp=headers["X-Slack-Request-Timestamp"],
                req_data=body,
            )

    def test_uses_hmac_compare_digest_for_constant_time_comparison(self):
        """verify_slack_request compares signatures via hmac.compare_digest (constant-time)."""
        body, headers = build_signed_slack_request("payload=ok")
        slack = _build_integration(build_slack_client_mock())
        with patch("hmac.compare_digest", return_value=True) as mock_cmp:
            slack.verify_slack_request(
                signature=headers["X-Slack-Signature"],
                timestamp=headers["X-Slack-Request-Timestamp"],
                req_data=body,
            )
        mock_cmp.assert_called_once()

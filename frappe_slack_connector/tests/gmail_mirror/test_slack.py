from frappe.tests import IntegrationTestCase
from slack_sdk.errors import SlackApiError

from frappe_slack_connector.gmail_mirror.slack import (
    FATAL_ERRORS,
    PROBLEM_MESSAGES,
    RATE_LIMIT_RETRIES,
    channel_problem,
    mirror_client,
    slack_error_code,
)
from frappe_slack_connector.tests import TEST_SLACK_CHANNEL_ID, build_slack_client_mock, set_password_field
from frappe_slack_connector.tests.gmail_mirror import api_error, channel_info


class TestChannelProblem(IntegrationTestCase):
    def test_healthy_channel(self):
        client = build_slack_client_mock()
        self.assertIsNone(channel_problem(client, TEST_SLACK_CHANNEL_ID))
        client.conversations_info.assert_called_once_with(channel=TEST_SLACK_CHANNEL_ID)

    def test_each_problem(self):
        cases = {
            "is_archived": {"is_archived": True},
            "ext_shared": {"is_ext_shared": True},
            "not_in_channel": {"is_member": False},
        }
        for expected, flags in cases.items():
            with self.subTest(problem=expected):
                client = build_slack_client_mock(conversations_info=channel_info(**flags))
                self.assertEqual(channel_problem(client, TEST_SLACK_CHANNEL_ID), expected)

    def test_channel_about_to_be_shared_counts_as_shared(self):
        client = build_slack_client_mock(conversations_info=channel_info(is_pending_ext_shared=True))
        self.assertEqual(channel_problem(client, TEST_SLACK_CHANNEL_ID), "ext_shared")

    def test_shared_channel_is_refused_even_when_the_bot_is_not_a_member(self):
        client = build_slack_client_mock(conversations_info=channel_info(is_ext_shared=True, is_member=False))
        self.assertEqual(channel_problem(client, TEST_SLACK_CHANNEL_ID), "ext_shared")

    def test_unknown_channel(self):
        client = build_slack_client_mock()
        client.conversations_info.side_effect = api_error("channel_not_found")
        self.assertEqual(channel_problem(client, "C0GONE0001"), "channel_not_found")

    def test_other_errors_propagate(self):
        client = build_slack_client_mock()
        client.conversations_info.side_effect = api_error("internal_error")
        with self.assertRaises(SlackApiError):
            channel_problem(client, TEST_SLACK_CHANNEL_ID)

    def test_every_problem_has_a_message_naming_the_channel(self):
        for code in ("channel_not_found", "is_archived", "ext_shared", "not_in_channel"):
            with self.subTest(problem=code):
                self.assertIn("#general", PROBLEM_MESSAGES[code].format("general"))


class TestMirrorClient(IntegrationTestCase):
    def test_waits_out_rate_limits_and_does_not_resend_after_a_dropped_connection(self):
        """Only the rate-limit handler: the SDK default would re-send a post Slack may already have accepted."""
        set_password_field("Slack Settings", "Slack Settings", "slack_bot_token", "xoxb-test")
        client = mirror_client()
        self.assertEqual(client.token, "xoxb-test")
        self.assertEqual([type(h).__name__ for h in client.retry_handlers], ["RateLimitErrorRetryHandler"])
        self.assertEqual(client.retry_handlers[0].max_retry_count, RATE_LIMIT_RETRIES)


class TestErrorCodes(IntegrationTestCase):
    def test_error_code(self):
        self.assertEqual(slack_error_code(api_error("ratelimited")), "ratelimited")
        self.assertIsNone(slack_error_code(ValueError("x")))

    def test_errors_a_retry_cannot_fix(self):
        for code in ("channel_not_found", "not_in_channel", "is_archived", "invalid_auth", "missing_scope"):
            self.assertIn(code, FATAL_ERRORS)
        for code in ("ratelimited", "internal_error", "fatal_error", "service_unavailable"):
            self.assertNotIn(code, FATAL_ERRORS)

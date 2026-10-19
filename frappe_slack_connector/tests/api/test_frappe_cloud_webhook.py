import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.frappe_cloud_webhook import receive

WEBHOOK_MODULE = "frappe_slack_connector.api.frappe_cloud_webhook"
TEST_WEBHOOK_SECRET = "test-frappe-cloud-secret"

DEPLOY_PAYLOAD = {
    "event": "Bench Deploy Status Update",
    "data": {"name": "deploy-0019-000059", "group": "bench-0019", "status": "Running"},
}


def _build_settings_mock(*, enabled=1, secret=TEST_WEBHOOK_SECRET):
    settings = MagicMock()
    settings.send_frappe_cloud_deploy_updates = enabled
    settings.get_password.return_value = secret
    return settings


def _call_receive(payload, *, secret=TEST_WEBHOOK_SECRET, settings=None):
    """Invoke receive() with a mocked request carrying the payload and secret header."""
    request = MagicMock()
    request.headers = {"X-Webhook-Secret": secret} if secret is not None else {}
    request.get_data.return_value = payload if isinstance(payload, str) else json.dumps(payload)
    with (
        patch(f"{WEBHOOK_MODULE}.frappe.request", request, create=True),
        patch(f"{WEBHOOK_MODULE}.frappe.get_single", return_value=settings or _build_settings_mock()),
        patch(f"{WEBHOOK_MODULE}.frappe.enqueue") as mock_enqueue,
    ):
        receive()
    return mock_enqueue


class TestFrappeCloudWebhook(IntegrationTestCase):
    def test_returns_403_when_secret_does_not_match(self):
        """receive rejects a request whose X-Webhook-Secret differs from the configured secret."""
        mock_enqueue = _call_receive(DEPLOY_PAYLOAD, secret="wrong-secret")
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_enqueue.assert_not_called()

    def test_returns_403_when_secret_header_missing(self):
        """receive rejects a request without the X-Webhook-Secret header."""
        mock_enqueue = _call_receive(DEPLOY_PAYLOAD, secret=None)
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_enqueue.assert_not_called()

    def test_returns_403_when_feature_disabled(self):
        """receive rejects requests when Frappe Cloud deploy updates are switched off."""
        mock_enqueue = _call_receive(DEPLOY_PAYLOAD, settings=_build_settings_mock(enabled=0))
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_enqueue.assert_not_called()

    def test_returns_403_when_no_secret_configured(self):
        """receive rejects requests when no webhook secret is set, even if the header is empty."""
        mock_enqueue = _call_receive(DEPLOY_PAYLOAD, secret="", settings=_build_settings_mock(secret=None))
        self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        mock_enqueue.assert_not_called()

    def test_returns_400_on_invalid_json(self):
        """receive answers 400 when the body is not a JSON object."""
        mock_enqueue = _call_receive("not json")
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)
        mock_enqueue.assert_not_called()

    def test_answers_validation_ping_without_queueing(self):
        """receive answers 200 to Frappe Cloud's Webhook Validate ping and queues nothing."""
        mock_enqueue = _call_receive({"event": "Webhook Validate", "data": {}})
        self.assertEqual(frappe.local.response.get("http_status_code"), 200)
        mock_enqueue.assert_not_called()

    def test_queues_event_for_background_processing(self):
        """receive answers 200 and queues process_event with the payload."""
        mock_enqueue = _call_receive(DEPLOY_PAYLOAD)
        self.assertEqual(frappe.local.response.get("http_status_code"), 200)
        mock_enqueue.assert_called_once()
        self.assertEqual(
            mock_enqueue.call_args.args[0],
            "frappe_slack_connector.tasks.frappe_cloud_deploy.process_event",
        )
        self.assertEqual(mock_enqueue.call_args.kwargs["payload"], DEPLOY_PAYLOAD)

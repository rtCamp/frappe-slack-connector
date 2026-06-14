from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.api.sync_slack_settings import (
    sync_slack_channels,
    sync_slack_channels_job,
    sync_slack_data,
    sync_slack_job,
)

SYNC_MODULE = "frappe_slack_connector.api.sync_slack_settings"


class TestSyncSlackData(IntegrationTestCase):
    def test_enqueues_sync_slack_job_with_notify_true(self):
        """sync_slack_data enqueues sync_slack_job on the long queue with notify=True."""
        with (
            patch(f"{SYNC_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{SYNC_MODULE}.frappe.msgprint"),
        ):
            sync_slack_data()
        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args.args
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIs(args[0], sync_slack_job)
        self.assertEqual(kwargs["queue"], "long")
        self.assertTrue(kwargs["notify"])


class TestSyncSlackJob(IntegrationTestCase):
    def test_inserts_user_meta_for_user_without_existing_row(self):
        """sync_slack_job inserts a new User Meta row when the email matches a frappe User but no User Meta exists yet."""
        mock_slack = MagicMock()
        mock_slack.get_slack_users.return_value = {
            "new@x.com": {"id": "U-NEW", "name": "new_user"},
        }
        mock_new_doc = MagicMock()
        with (
            patch(f"{SYNC_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SYNC_MODULE}.frappe.get_all", return_value=[]),
            patch(
                f"{SYNC_MODULE}.frappe.db.exists",
                side_effect=lambda dt, key: dt == "User" and key == "new@x.com",
            ),
            patch(f"{SYNC_MODULE}.frappe.get_doc", return_value=mock_new_doc) as mock_get_doc,
            patch(f"{SYNC_MODULE}.frappe.db.commit"),
            patch(f"{SYNC_MODULE}.frappe.msgprint"),
        ):
            sync_slack_job(notify=False)
        # Verify the new-doc dict shape for User Meta insert.
        insert_calls = [c for c in mock_get_doc.call_args_list if c.args[0].get("doctype") == "User Meta"]
        self.assertEqual(len(insert_calls), 1)
        doc_data = insert_calls[0].args[0]
        self.assertEqual(doc_data["user"], "new@x.com")
        self.assertEqual(doc_data["custom_slack_userid"], "U-NEW")
        mock_new_doc.insert.assert_called_once_with(ignore_permissions=True)

    def test_updates_existing_user_meta_row_via_db_set_value(self):
        """When User Meta already exists for the email, sync_slack_job updates it via db.set_value (no insert)."""
        mock_slack = MagicMock()
        mock_slack.get_slack_users.return_value = {
            "old@x.com": {"id": "U-OLD-NEW", "name": "old_user"},
        }
        existing_user_metas = [frappe._dict({"name": "UM-001", "user": "old@x.com"})]

        # sync_slack_job calls frappe.get_all twice (User Meta + Employee). Route by doctype
        # so the Employee branch sees an empty list and doesn't fall into the error-handling path.
        def get_all_router(doctype, *args, **kwargs):
            if doctype == "User Meta":
                return existing_user_metas
            if doctype == "Employee":
                return [frappe._dict({"user_id": "old@x.com", "employee_name": "Old"})]
            return []

        mock_new_doc = MagicMock()
        with (
            patch(f"{SYNC_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SYNC_MODULE}.frappe.get_all", side_effect=get_all_router),
            patch(f"{SYNC_MODULE}.frappe.db.set_value") as mock_set_value,
            patch(f"{SYNC_MODULE}.frappe.get_doc", return_value=mock_new_doc),
            patch(f"{SYNC_MODULE}.frappe.db.commit"),
            patch(f"{SYNC_MODULE}.frappe.msgprint"),
            patch(f"{SYNC_MODULE}.generate_error_log") as mock_log,
        ):
            sync_slack_job(notify=False)
        mock_set_value.assert_called_once()
        # Update path was taken; the User Meta insert helper on mock_new_doc was not invoked.
        mock_new_doc.insert.assert_not_called()
        # And the error-handling path was not triggered.
        mock_log.assert_not_called()

    def test_msgprints_realtime_success_when_notify_true(self):
        """When notify=True, sync_slack_job emits a realtime msgprint after a successful sync."""
        mock_slack = MagicMock()
        mock_slack.get_slack_users.return_value = {
            "x@y.com": {"id": "U", "name": "x"},
        }
        with (
            patch(f"{SYNC_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SYNC_MODULE}.frappe.get_all", return_value=[]),
            patch(f"{SYNC_MODULE}.frappe.db.exists", return_value=False),
            patch(f"{SYNC_MODULE}.frappe.db.commit"),
            patch(f"{SYNC_MODULE}.frappe.msgprint") as mock_msgprint,
        ):
            sync_slack_job(notify=True)
        # At least one realtime success msgprint should have fired.
        realtime_calls = [
            c for c in mock_msgprint.call_args_list if c.kwargs.get("realtime") and c.kwargs.get("indicator") == "green"
        ]
        self.assertTrue(realtime_calls, "expected at least one realtime success msgprint")


class TestSyncSlackChannels(IntegrationTestCase):
    def test_enqueues_sync_slack_channels_job_with_notify_true(self):
        """sync_slack_channels enqueues sync_slack_channels_job on the long queue with notify=True."""
        with (
            patch(f"{SYNC_MODULE}.frappe.enqueue") as mock_enqueue,
            patch(f"{SYNC_MODULE}.frappe.msgprint"),
        ):
            sync_slack_channels()
        args = mock_enqueue.call_args.args
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIs(args[0], sync_slack_channels_job)
        self.assertEqual(kwargs["queue"], "long")
        self.assertTrue(kwargs["notify"])


class TestSyncSlackChannelsJob(IntegrationTestCase):
    def test_inserts_slack_channel_row_for_new_channel(self):
        """sync_slack_channels_job inserts a new Slack Channel doc for every channel not already in the doctype."""
        mock_slack = MagicMock()
        mock_slack.get_slack_channels.return_value = [
            {"id": "C-NEW", "name": "new-channel"},
        ]
        mock_new_doc = MagicMock()
        with (
            patch(f"{SYNC_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SYNC_MODULE}.frappe.db.get_all", return_value=[]),
            patch(f"{SYNC_MODULE}.frappe.get_doc", return_value=mock_new_doc) as mock_get_doc,
            patch(f"{SYNC_MODULE}.frappe.db.commit"),
            patch(f"{SYNC_MODULE}.frappe.msgprint"),
        ):
            sync_slack_channels_job(notify=False)
        mock_get_doc.assert_called_once()
        doc_data = mock_get_doc.call_args.args[0]
        self.assertEqual(doc_data["doctype"], "Slack Channel")
        self.assertEqual(doc_data["channel_id"], "C-NEW")
        self.assertEqual(doc_data["channel_name"], "new-channel")
        mock_new_doc.insert.assert_called_once_with(ignore_permissions=True)

    def test_msgprints_realtime_success_with_channel_count_when_notify_true(self):
        """When notify=True, sync_slack_channels_job emits a realtime msgprint reporting the synced channel count."""
        mock_slack = MagicMock()
        mock_slack.get_slack_channels.return_value = [
            {"id": "C-1", "name": "one"},
            {"id": "C-2", "name": "two"},
        ]
        with (
            patch(f"{SYNC_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{SYNC_MODULE}.frappe.db.get_all", return_value=[]),
            patch(f"{SYNC_MODULE}.frappe.get_doc", return_value=MagicMock()),
            patch(f"{SYNC_MODULE}.frappe.db.commit"),
            patch(f"{SYNC_MODULE}.frappe.msgprint") as mock_msgprint,
        ):
            sync_slack_channels_job(notify=True)
        success_calls = [
            c for c in mock_msgprint.call_args_list if c.kwargs.get("realtime") and c.kwargs.get("indicator") == "green"
        ]
        self.assertEqual(len(success_calls), 1)

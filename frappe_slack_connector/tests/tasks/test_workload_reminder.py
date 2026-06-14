from datetime import date as date_cls
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.tasks.workload_reminder import (
    get_mention_cell,
    get_mention_text,
    send_blocks_in_chunks,
    send_daily_workload_reminder,
    send_weekly_workload_reminder,
)

WORKLOAD_MODULE = "frappe_slack_connector.tasks.workload_reminder"


def _build_settings_mock(
    *,
    send_daily_allocation_updates=1,
    send_weekly_allocation_updates=1,
    workload_channel_id="#workload",
    workload_mention_users=0,
):
    settings = MagicMock()
    settings.send_daily_allocation_updates = send_daily_allocation_updates
    settings.send_weekly_allocation_updates = send_weekly_allocation_updates
    settings.workload_channel_id = workload_channel_id
    settings.workload_mention_users = workload_mention_users
    return settings


class TestSendDailyWorkloadReminder(IntegrationTestCase):
    def test_returns_silently_when_daily_updates_disabled(self):
        """send_daily_workload_reminder returns silently when send_daily_allocation_updates=0."""
        settings = _build_settings_mock(send_daily_allocation_updates=0)
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=True),
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
        ):
            send_daily_workload_reminder()
        mock_slack_class.assert_not_called()

    def test_logs_error_and_returns_when_next_pms_not_installed(self):
        """send_daily_workload_reminder logs an error and returns when next_pms is not installed."""
        settings = _build_settings_mock(send_daily_allocation_updates=1)
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
            patch(f"{WORKLOAD_MODULE}.generate_error_log") as mock_log,
        ):
            send_daily_workload_reminder()
        mock_log.assert_called_once()
        mock_slack_class.assert_not_called()

    def test_returns_silently_when_today_is_weekend(self):
        """send_daily_workload_reminder returns silently when today is Saturday or Sunday."""
        settings = _build_settings_mock(send_daily_allocation_updates=1)
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=True),
            patch(f"{WORKLOAD_MODULE}.getdate", return_value=date_cls(2026, 6, 13)),  # Saturday
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
        ):
            send_daily_workload_reminder()
        mock_slack_class.assert_not_called()

    def test_returns_silently_when_no_underallocated_employees(self):
        """send_daily_workload_reminder returns silently when no employees are underallocated."""
        settings = _build_settings_mock(send_daily_allocation_updates=1)
        mock_slack = MagicMock()
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=True),
            patch(f"{WORKLOAD_MODULE}.getdate", return_value=date_cls(2026, 6, 15)),  # Monday
            patch(f"{WORKLOAD_MODULE}.SlackIntegration", return_value=mock_slack),
            patch(f"{WORKLOAD_MODULE}.get_workload_data", return_value=([], {}, {})),
        ):
            send_daily_workload_reminder()
        mock_slack.slack_app.client.chat_postMessage.assert_not_called()


class TestSendWeeklyWorkloadReminder(IntegrationTestCase):
    def test_returns_silently_when_weekly_updates_disabled(self):
        """send_weekly_workload_reminder returns silently when send_weekly_allocation_updates=0."""
        settings = _build_settings_mock(send_weekly_allocation_updates=0)
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=True),
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
        ):
            send_weekly_workload_reminder()
        mock_slack_class.assert_not_called()

    def test_logs_error_and_returns_when_next_pms_not_installed(self):
        """send_weekly_workload_reminder logs an error and returns when next_pms is not installed."""
        settings = _build_settings_mock(send_weekly_allocation_updates=1)
        with (
            patch(f"{WORKLOAD_MODULE}.frappe.get_single", return_value=settings),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=False),
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
            patch(f"{WORKLOAD_MODULE}.generate_error_log") as mock_log,
        ):
            send_weekly_workload_reminder()
        mock_log.assert_called_once()
        mock_slack_class.assert_not_called()

    def test_returns_silently_when_today_is_not_remind_day(self):
        """send_weekly_workload_reminder returns silently when today's weekday doesn't match Timesheet Settings.remind_on."""
        settings = _build_settings_mock(send_weekly_allocation_updates=1)
        ts_settings = MagicMock()
        ts_settings.remind_on = "Monday"

        def get_single_router(doctype):
            return settings if doctype == "Slack Settings" else ts_settings

        with (
            patch(
                f"{WORKLOAD_MODULE}.frappe.get_single",
                side_effect=get_single_router,
            ),
            patch(f"{WORKLOAD_MODULE}.is_next_pms_installed", return_value=True),
            patch(f"{WORKLOAD_MODULE}.getdate", return_value=date_cls(2026, 6, 17)),
            patch(f"{WORKLOAD_MODULE}.get_weekday", return_value="Wednesday"),
            patch(f"{WORKLOAD_MODULE}.SlackIntegration") as mock_slack_class,
        ):
            send_weekly_workload_reminder()
        mock_slack_class.assert_not_called()


class TestSendBlocksInChunks(IntegrationTestCase):
    def test_chunks_blocks_at_50_per_chat_postMessage_call(self):
        """send_blocks_in_chunks posts blocks 50 at a time when the list exceeds 50 entries."""
        slack = MagicMock()
        blocks = [{"type": "section", "block_id": f"b-{i}"} for i in range(125)]
        send_blocks_in_chunks(slack, "#channel", blocks)
        # 125 blocks / 50 = ceil(2.5) = 3 calls.
        self.assertEqual(slack.slack_app.client.chat_postMessage.call_count, 3)
        sizes = [len(c.kwargs["blocks"]) for c in slack.slack_app.client.chat_postMessage.call_args_list]
        self.assertEqual(sizes, [50, 50, 25])

    def test_posts_single_call_when_blocks_under_chunk_limit(self):
        """send_blocks_in_chunks posts a single call when the blocks list is under 50."""
        slack = MagicMock()
        blocks = [{"type": "section", "block_id": f"b-{i}"} for i in range(7)]
        send_blocks_in_chunks(slack, "#channel", blocks)
        slack.slack_app.client.chat_postMessage.assert_called_once()
        self.assertEqual(
            len(slack.slack_app.client.chat_postMessage.call_args.kwargs["blocks"]),
            7,
        )


class TestMentionHelpers(IntegrationTestCase):
    def test_get_mention_text_includes_slack_mention_when_id_present(self):
        """get_mention_text returns '<name> (<@slack_id>)' when a slack_id is supplied."""
        result = get_mention_text("U-001", "Alice")
        self.assertEqual(result, "Alice (<@U-001>)")

    def test_get_mention_text_returns_name_only_when_no_slack_id(self):
        """get_mention_text returns just the fallback name when slack_id is None."""
        self.assertEqual(get_mention_text(None, "Alice"), "Alice")

    def test_get_mention_cell_returns_rich_text_user_element_when_slack_id_present(self):
        """get_mention_cell returns a rich_text dict containing a user element when slack_id is supplied."""
        result = get_mention_cell("U-001", "Alice")
        self.assertEqual(result["type"], "rich_text")
        user_elements = [e for e in result["elements"][0]["elements"] if e.get("type") == "user"]
        self.assertEqual(user_elements[0]["user_id"], "U-001")

    def test_get_mention_cell_returns_raw_text_when_no_slack_id(self):
        """get_mention_cell returns a raw_text dict with the fallback name when slack_id is None."""
        result = get_mention_cell(None, "Alice")
        self.assertEqual(result, {"type": "raw_text", "text": "Alice"})

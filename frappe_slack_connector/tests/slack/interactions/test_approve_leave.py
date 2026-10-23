import contextlib
from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.workflow import WorkflowTransitionError
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.approve_leave import handler
from frappe_slack_connector.tests import TEST_SLACK_USER_ID, TEST_USER

APPROVE_LEAVE_MODULE = "frappe_slack_connector.slack.interactions.approve_leave"


def _leave_row(status="Open", docstatus=0, modified_by="approver@x.com", modified="2026-06-10 10:00:00"):
    """What frappe.db.get_value returns for the leave the button refers to."""
    return frappe._dict(
        status=status, docstatus=docstatus, modified_by=modified_by, modified=modified, leave_approver="approver@x.com"
    )


@contextlib.contextmanager
def _patch_leave(row=None, *, get_value=None):
    """Patch the leave lookup (``row``, or a ``get_value`` side effect) and pin the site timezone: patching
    frappe.db.get_value globally would otherwise break the timezone lookup behind the decision-time token."""
    with contextlib.ExitStack() as stack:
        if get_value is not None:
            stack.enter_context(patch(f"{APPROVE_LEAVE_MODULE}.frappe.db.get_value", side_effect=get_value))
        else:
            stack.enter_context(patch(f"{APPROVE_LEAVE_MODULE}.frappe.db.get_value", return_value=row or _leave_row()))
        stack.enter_context(
            patch("frappe_slack_connector.tasks.attendance_summary.get_system_timezone", return_value="Asia/Kolkata")
        )
        # now_datetime() also reads the site timezone through frappe.db.get_value
        stack.enter_context(
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.utils.now_datetime", return_value=datetime(2026, 6, 10, 10, 0))
        )
        yield


def _build_action_payload(*, action_id="leave_approve", leave_id="HR-LAP-0001"):
    """Build a Slack block_actions payload mimicking the Approve/Reject button click."""
    return {
        "user": {"id": TEST_SLACK_USER_ID},
        "trigger_id": "T-trigger-1",
        "channel": {"id": "C-channel-1"},
        "container": {"message_ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": leave_id}],
        "message": {
            "blocks": [
                {"block_id": "header_block", "type": "header"},
                {"block_id": "details_block", "type": "section"},
                {"block_id": "leave_actions_block", "type": "actions"},
                {"block_id": "footer_block", "type": "context"},
            ]
        },
    }


class TestApproveLeaveHandler(IntegrationTestCase):
    def test_resolves_slack_approver_and_calls_approve_leave(self):
        """handler resolves the Slack user to a Frappe user via get_userid_from_slackid, sets frappe.session.user, and calls approve_leave."""
        payload = _build_action_payload(action_id="leave_approve", leave_id="HR-LAP-0010")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user") as mock_set_user,
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave") as mock_reject,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_set_user.assert_called_once_with(TEST_USER)
        mock_approve.assert_called_once_with("HR-LAP-0010")
        mock_reject.assert_not_called()

    def test_rejects_leave_when_action_id_is_leave_reject(self):
        """handler calls reject_leave (not approve_leave) when action_id is leave_reject."""
        payload = _build_action_payload(action_id="leave_reject", leave_id="HR-LAP-0011")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave") as mock_reject,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_reject.assert_called_once_with("HR-LAP-0011")
        mock_approve.assert_not_called()

    def test_chat_update_replaces_action_block_with_approved_status(self):
        """After a successful approve, handler calls chat_update with the leave_actions_block replaced by a 'Approved' status section."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave"),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.chat_update.assert_called_once()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C-channel-1")
        self.assertEqual(kwargs["ts"], "1700000000.000100")
        # leave_actions_block is now the status line (a context block with the decision time), footer_block is removed
        block_ids = [b.get("block_id") for b in kwargs["blocks"]]
        self.assertNotIn("leave_actions_block", block_ids)
        self.assertNotIn("footer_block", block_ids)
        status_line = kwargs["blocks"][2]["elements"][0]["text"]
        self.assertIn("*Status:* :white_check_mark: *Approved* · _<!date^", status_line)
        self.assertEqual(kwargs["text"], "Leave request approved")

    def test_chat_update_uses_rejected_status_for_reject_action(self):
        """For a leave_reject action, the chat_update status block reads 'Rejected'."""
        payload = _build_action_payload(action_id="leave_reject")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave"),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        status_line = next(b["elements"][0]["text"] for b in kwargs["blocks"] if b.get("type") == "context")
        self.assertIn("*Status:* :x: *Rejected*", status_line)

    def test_opens_error_modal_when_db_update_raises(self):
        """handler opens an error modal via views_open when the approve/reject DB call raises."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave", side_effect=RuntimeError("boom")),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave"),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.views_open.assert_called_once()
        kwargs = mock_slack.slack_app.client.views_open.call_args.kwargs
        self.assertEqual(kwargs["trigger_id"], "T-trigger-1")
        self.assertEqual(kwargs["view"]["title"]["text"], "Error")
        mock_slack.slack_app.client.chat_update.assert_not_called()

    def test_stale_click_on_a_decided_leave_reports_that_decision_without_acting(self):
        """Approve clicked after the leave was auto-approved (Administrator) or decided in Desk: no workflow call, the message shows the real decision, who took it and when."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        row = _leave_row(status="Approved", docstatus=1, modified_by="Administrator", modified="2026-06-11 03:00:00")
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(row),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}._format_decider", return_value="Administrator") as mock_decider,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_approve.assert_not_called()
        mock_slack.slack_app.client.views_open.assert_not_called()
        mock_decider.assert_called_once_with(mock_slack, "Administrator")
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        status_line = next(b["elements"][0]["text"] for b in kwargs["blocks"] if b.get("type") == "context")
        self.assertIn("*Status:* :white_check_mark: *Approved* by Administrator · _<!date^", status_line)
        self.assertTrue(status_line.endswith("|Jun 11, 2026 at 3:00 AM>_"))
        self.assertNotIn("leave_actions_block", [b.get("block_id") for b in kwargs["blocks"]])

    def test_stale_click_on_a_cancelled_leave_shows_cancelled(self):
        """A cancelled leave (docstatus 2) reads Cancelled whatever its status column says; Reject is not attempted."""
        payload = _build_action_payload(action_id="leave_reject")
        mock_slack = MagicMock()
        row = _leave_row(status="Approved", docstatus=2, modified_by="alice@x.com")
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(row),
            patch(f"{APPROVE_LEAVE_MODULE}.reject_leave") as mock_reject,
            patch(f"{APPROVE_LEAVE_MODULE}._format_decider", return_value="Alice"),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_reject.assert_not_called()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        status_line = next(b["elements"][0]["text"] for b in kwargs["blocks"] if b.get("type") == "context")
        self.assertIn("*Status:* :no_entry_sign: *Cancelled* by Alice", status_line)
        self.assertEqual(kwargs["text"], "Leave request cancelled")

    def _race(self, error):
        """The leave is open at the read but decided before the action lands: ``error`` is what the action raises."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        rows = iter([_leave_row(), _leave_row(status="Rejected", docstatus=1, modified_by="bob@x.com")])
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(get_value=lambda *a, **k: next(rows)),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.db.rollback") as mock_rollback,
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave", side_effect=error),
            patch(f"{APPROVE_LEAVE_MODULE}._format_decider", return_value="<@U-bob>"),
        ):
            handler(slack=mock_slack, payload=payload)
        # The second read must come after the transaction ends: under REPEATABLE READ it would otherwise
        # see the same snapshot as the first one and never the decision that won the race
        mock_rollback.assert_called_once()
        mock_slack.slack_app.client.views_open.assert_not_called()
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        status_line = next(b["elements"][0]["text"] for b in kwargs["blocks"] if b.get("type") == "context")
        self.assertIn("*Status:* :x: *Rejected* by <@U-bob>", status_line)

    def test_race_lost_in_the_workflow_reports_the_other_decision(self):
        """apply_workflow loads the new state and finds no Approve transition."""
        self._race(WorkflowTransitionError("Not a valid Workflow Action"))

    def test_race_lost_in_the_save_reports_the_other_decision(self):
        """The row changed after the workflow's non-locking load: save() raises TimestampMismatchError."""
        self._race(frappe.TimestampMismatchError("Leave Application has been modified after you have opened it"))

    def test_race_that_leaves_the_leave_open_still_raises(self):
        """A workflow error on a leave that is still open is a real error: the modal shows it."""
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.db.rollback"),
            patch(
                f"{APPROVE_LEAVE_MODULE}.approve_leave",
                side_effect=WorkflowTransitionError("Not a valid Workflow Action"),
            ),
        ):
            handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.chat_update.assert_not_called()
        view = mock_slack.slack_app.client.views_open.call_args.kwargs["view"]
        self.assertIn("Not a valid Workflow Action", view["blocks"][2]["text"]["text"])

    def test_stale_click_with_no_recorded_user_falls_back_to_the_approver(self):
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        row = _leave_row(status="Approved", docstatus=1, modified_by=None)
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(row),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
            patch(f"{APPROVE_LEAVE_MODULE}._format_decider", return_value="<@U-approver>") as mock_decider,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_approve.assert_not_called()
        mock_decider.assert_called_once_with(mock_slack, "approver@x.com")

    def test_stale_click_on_a_cancelled_leave_with_no_recorded_user_names_nobody(self):
        payload = _build_action_payload(action_id="leave_approve")
        mock_slack = MagicMock()
        row = _leave_row(status="Cancelled", docstatus=2, modified_by=None)
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(row),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave"),
            patch(f"{APPROVE_LEAVE_MODULE}._format_decider", return_value=None) as mock_decider,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_decider.assert_called_once_with(mock_slack, None)
        kwargs = mock_slack.slack_app.client.chat_update.call_args.kwargs
        status_line = next(b["elements"][0]["text"] for b in kwargs["blocks"] if b.get("type") == "context")
        self.assertTrue(status_line.startswith("*Status:* :no_entry_sign: *Cancelled* · _<!date^"))

    def test_unknown_leave_opens_the_error_modal(self):
        payload = _build_action_payload(action_id="leave_approve", leave_id="HR-LAP-GONE")
        mock_slack = MagicMock()
        with (
            patch(f"{APPROVE_LEAVE_MODULE}.get_userid_from_slackid", return_value=TEST_USER),
            patch(f"{APPROVE_LEAVE_MODULE}.frappe.set_user"),
            _patch_leave(get_value=lambda *a, **k: None),
            patch(f"{APPROVE_LEAVE_MODULE}.approve_leave") as mock_approve,
        ):
            handler(slack=mock_slack, payload=payload)
        mock_approve.assert_not_called()
        view = mock_slack.slack_app.client.views_open.call_args.kwargs["view"]
        self.assertIn("Leave Application HR-LAP-GONE not found", view["blocks"][2]["text"]["text"])
        mock_slack.slack_app.client.chat_update.assert_not_called()

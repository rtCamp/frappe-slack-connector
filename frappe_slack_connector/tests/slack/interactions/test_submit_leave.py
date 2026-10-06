from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.slack.interactions.submit_leave import (
    half_day_checkbox_handler,
    handler,
)

SUBMIT_LEAVE_MODULE = "frappe_slack_connector.slack.interactions.submit_leave"


def _build_submission_payload(
    *,
    is_half_day=False,
    start_date="2026-06-01",
    end_date="2026-06-05",
    leave_type="Casual Leave",
    reason="vacation",
):
    """Build a Slack view_submission payload mimicking the leave-modal form fields."""
    view_state = {
        "start_date": {"start_date_picker": {"selected_date": start_date}},
        "end_date": {"end_date_picker": {"selected_date": end_date}},
        "leave_type": {"leave_type_select": {"selected_option": {"value": leave_type}}},
        "reason": {"reason_input": {"value": reason}},
        "half_day_checkbox": {
            "half_day_checkbox": {"selected_options": [{"value": "half_day"}] if is_half_day else []}
        },
    }
    if is_half_day:
        view_state["half_day_date"] = {"half_day_date_picker": {"selected_date": start_date}}
    return {
        "user": {"id": "U001"},
        "view": {"state": {"values": view_state}},
    }


def _build_checkbox_payload(
    *,
    half_day_selected,
    start_date="2026-06-01",
    end_date="2026-06-05",
    existing_blocks=None,
):
    """Build a Slack block_actions payload for the half-day checkbox toggle."""
    blocks = existing_blocks or [
        {"block_id": "start_date", "type": "input"},
        {"block_id": "end_date", "type": "input"},
        {"block_id": "leave_type", "type": "input"},
        {"block_id": "reason", "type": "input"},
        {"block_id": "half_day_checkbox", "type": "actions"},
    ]
    return {
        "view": {
            "id": "V001",
            "hash": "H001",
            "callback_id": "apply_leave_application",
            "title": {"type": "plain_text", "text": "Apply for Leave"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": blocks,
            "state": {
                "values": {
                    "start_date": {"start_date_picker": {"selected_date": start_date}},
                    "end_date": {"end_date_picker": {"selected_date": end_date}},
                    "half_day_checkbox": {
                        "half_day_checkbox": {"selected_options": [{"value": "half_day"}] if half_day_selected else []}
                    },
                }
            },
        }
    }


class TestSubmitLeaveHandler(IntegrationTestCase):
    def test_creates_leave_application_with_open_status_and_parsed_fields(self):
        """handler creates a Leave Application doc with employee, leave_type, from_date, to_date, description, and status='Open' parsed from view.state.values."""
        payload = _build_submission_payload()
        mock_leave_app = MagicMock()
        with (
            patch(f"{SUBMIT_LEAVE_MODULE}.frappe.get_doc", return_value=mock_leave_app) as mock_get_doc,
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_leave_approver",
                return_value="approver@x.com",
            ),
            patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{SUBMIT_LEAVE_MODULE}.frappe.db.commit"),
            patch(f"{SUBMIT_LEAVE_MODULE}.clear_messages"),
        ):
            handler(slack=MagicMock(), payload=payload)
        doc_data = mock_get_doc.call_args.args[0]
        self.assertEqual(doc_data["doctype"], "Leave Application")
        self.assertEqual(doc_data["employee"], "EMP-001")
        self.assertEqual(doc_data["leave_type"], "Casual Leave")
        self.assertEqual(doc_data["from_date"], "2026-06-01")
        self.assertEqual(doc_data["to_date"], "2026-06-05")
        self.assertEqual(doc_data["description"], "vacation")
        self.assertEqual(doc_data["status"], "Open")
        self.assertEqual(doc_data["leave_approver"], "approver@x.com")
        mock_leave_app.save.assert_called_once_with(ignore_permissions=True)

    def test_sets_half_day_fields_when_checkbox_checked(self):
        """When is_half_day is checked, handler sets half_day=1 and half_day_date on the Leave Application doc."""
        payload = _build_submission_payload(is_half_day=True)
        mock_leave_app = MagicMock()
        with (
            patch(f"{SUBMIT_LEAVE_MODULE}.frappe.get_doc", return_value=mock_leave_app),
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_leave_approver",
                return_value="approver@x.com",
            ),
            patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{SUBMIT_LEAVE_MODULE}.frappe.db.commit"),
            patch(f"{SUBMIT_LEAVE_MODULE}.clear_messages"),
        ):
            handler(slack=MagicMock(), payload=payload)
        self.assertEqual(mock_leave_app.half_day, 1)
        self.assertEqual(mock_leave_app.half_day_date, "2026-06-01")

    def test_returns_error_modal_when_save_raises(self):
        """When the Leave Application save raises, handler returns an HTTP response containing an error modal payload."""
        payload = _build_submission_payload()
        mock_leave_app = MagicMock()
        mock_leave_app.save.side_effect = RuntimeError("boom")
        with (
            patch(f"{SUBMIT_LEAVE_MODULE}.frappe.get_doc", return_value=mock_leave_app),
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_employeeid_from_slackid",
                return_value="EMP-001",
            ),
            patch(
                f"{SUBMIT_LEAVE_MODULE}.get_leave_approver",
                return_value="approver@x.com",
            ),
            patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False),
            patch(f"{SUBMIT_LEAVE_MODULE}.send_http_response") as mock_response,
        ):
            handler(slack=MagicMock(), payload=payload)
        mock_response.assert_called_once()
        body = mock_response.call_args.kwargs["body"]
        self.assertEqual(body["response_action"], "push")
        self.assertEqual(body["view"]["title"]["text"], "Error")


class TestHalfDayCheckboxHandler(IntegrationTestCase):
    def test_adds_half_day_date_block_when_checked_and_dates_differ(self):
        """When is_half_day is checked and start_date != end_date, the handler appends a half_day_date block to the modal."""
        payload = _build_checkbox_payload(half_day_selected=True, start_date="2026-06-01", end_date="2026-06-05")
        mock_slack = MagicMock()
        with patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False):
            half_day_checkbox_handler(slack=mock_slack, payload=payload)
        mock_slack.slack_app.client.views_update.assert_called_once()
        view = mock_slack.slack_app.client.views_update.call_args.kwargs["view"]
        block_ids = [b["block_id"] for b in view["blocks"]]
        self.assertIn("half_day_date", block_ids)

    def test_does_not_add_half_day_date_block_when_dates_are_same(self):
        """When is_half_day is checked but start_date == end_date, the half_day_date block is not added."""
        payload = _build_checkbox_payload(half_day_selected=True, start_date="2026-06-01", end_date="2026-06-01")
        mock_slack = MagicMock()
        with patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False):
            half_day_checkbox_handler(slack=mock_slack, payload=payload)
        view = mock_slack.slack_app.client.views_update.call_args.kwargs["view"]
        block_ids = [b["block_id"] for b in view["blocks"]]
        self.assertNotIn("half_day_date", block_ids)

    def test_removes_half_day_blocks_when_unchecked(self):
        """When is_half_day is unchecked, the handler strips half_day_date and half_day_period blocks from the modal."""
        existing_blocks = [
            {"block_id": "start_date", "type": "input"},
            {"block_id": "end_date", "type": "input"},
            {"block_id": "leave_type", "type": "input"},
            {"block_id": "reason", "type": "input"},
            {"block_id": "half_day_checkbox", "type": "actions"},
            {"block_id": "half_day_date", "type": "input"},
            {"block_id": "half_day_period", "type": "input"},
        ]
        payload = _build_checkbox_payload(half_day_selected=False, existing_blocks=existing_blocks)
        mock_slack = MagicMock()
        with patch(f"{SUBMIT_LEAVE_MODULE}.custom_fields_exist", return_value=False):
            half_day_checkbox_handler(slack=mock_slack, payload=payload)
        view = mock_slack.slack_app.client.views_update.call_args.kwargs["view"]
        block_ids = [b["block_id"] for b in view["blocks"]]
        self.assertNotIn("half_day_date", block_ids)
        self.assertNotIn("half_day_period", block_ids)

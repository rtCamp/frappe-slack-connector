import frappe
from frappe.utils import today
from hrms.hr.doctype.leave_application.leave_application import (
    get_leave_allocation_records,
)

from frappe_slack_connector.db.user_meta import get_employeeid_from_slackid
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.http_response import send_http_response
from frappe_slack_connector.slack.app import SlackIntegration


@frappe.whitelist(allow_guest=True)
def slash_leave():
    slack = SlackIntegration()
    try:
        slack.verify_slack_request(
            signature=frappe.request.headers.get("X-Slack-Signature"),
            timestamp=frappe.request.headers.get("X-Slack-Request-Timestamp"),
            req_data=frappe.request.get_data(as_text=True),
        )
    except Exception:
        return send_http_response("Invalid request", status_code=403)

    try:
        employee_id = get_employeeid_from_slackid(frappe.form_dict.get("user_id"))
        leaves = get_leave_allocation_records(employee_id, today())

        slack.slack_app.client.views_open(
            trigger_id=frappe.form_dict.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": "apply_leave_application",
                "title": {"type": "plain_text", "text": "Apply for Leave"},
                "blocks": build_leave_form(leaves.keys()),
                "submit": {
                    "type": "plain_text",
                    "text": "Submit",
                },
            },
        )

        return send_http_response(
            status_code=204,
            is_empty=True,
        )
    except Exception as e:
        generate_error_log("Error opening modal", exception=e)
        slack.slack_app.client.views_open(
            trigger_id=frappe.form_dict.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": "error_modal",
                "title": {"type": "plain_text", "text": "Error"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "```An error occurred, please try again later.```",
                        },
                    }
                ],
                "close": {
                    "type": "plain_text",
                    "text": "Close",
                },
            },
        )
        return send_http_response(
            status_code=204,
            is_empty=True,
        )


def build_leave_form(leaves: list) -> list:
    blocks = [
        {
            "type": "input",
            "block_id": "start_date",
            "element": {
                "type": "datepicker",
                "action_id": "start_date_picker",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a date",
                },
                "initial_date": today(),
            },
            "label": {"type": "plain_text", "text": "Start Date"},
        },
        {
            "type": "input",
            "block_id": "end_date",
            "element": {
                "type": "datepicker",
                "action_id": "end_date_picker",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a date",
                },
                "initial_date": today(),
            },
            "label": {"type": "plain_text", "text": "End Date"},
        },
        {
            "type": "input",
            "block_id": "leave_type",
            "element": {
                "type": "static_select",
                "action_id": "leave_type_select",
                "options": [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": leave_type,
                        },
                        "value": leave_type,
                    }
                    for leave_type in leaves
                ],
            },
            "label": {"type": "plain_text", "text": "Leave Type"},
        },
        {
            "type": "input",
            "block_id": "reason",
            "element": {
                "type": "plain_text_input",
                "action_id": "reason_input",
                "multiline": True,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Enter reason for leave",
                },
            },
            "label": {"type": "plain_text", "text": "Reason"},
        },
        {
            "type": "actions",
            "block_id": "half_day_checkbox",
            "elements": [
                {
                    "type": "checkboxes",
                    "action_id": "half_day_checkbox",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Half Day",
                            },
                            "value": "half_day",
                        }
                    ],
                }
            ],
        },
    ]
    return blocks

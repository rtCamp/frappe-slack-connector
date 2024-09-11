import frappe
from frappe import _
from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

from frappe_slack_connector.db.user_meta import get_employeeid_from_slackid
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.slack.app import SlackIntegration


def handler(slack: SlackIntegration, payload: dict):
    generate_error_log("Approve Leave Handler", message=payload)
    if not payload:
        frappe.throw(_("No payload found"), frappe.ValidationError)
    try:
        # Extract relevant information
        user_info = payload["user"]
        view_state = payload["view"]["state"]["values"]

        # Extract leave details
        start_date = view_state["start_date"]["start_date_picker"]["selected_date"]
        end_date = view_state["end_date"]["end_date_picker"]["selected_date"]
        leave_type = view_state["leave_type"]["leave_type_select"]["selected_option"][
            "value"
        ]
        reason = view_state["reason"]["reason_input"]["value"]

        # Check if it's a half day
        is_half_day = (
            len(
                view_state["half_day_checkbox"]["half_day_checkbox"]["selected_options"]
            )
            > 0
        )
        half_day_period = None
        if is_half_day:
            half_day_period = view_state["half_day_period"]["half_day_period_select"][
                "selected_option"
            ]["value"]

        # Get the employee based on the Slack user ID
        employee = get_employeeid_from_slackid(user_info["id"])
        if not employee:
            frappe.throw(
                _("No employee found for this Slack user"), frappe.ValidationError
            )

        # Create the leave application
        leave_application = frappe.get_doc(
            {
                "doctype": "Leave Application",
                "employee": employee,
                "leave_type": leave_type,
                "from_date": start_date,
                "leave_approver": get_leave_approver(employee),
                "to_date": end_date,
                "status": "Open",
                "description": reason,
            }
        )

        if is_half_day:
            leave_application.half_day = 1
            leave_application.half_day_date = (
                start_date  # Assuming half day is on the start date
            )
            leave_application.half_day_period = (
                "First Half" if half_day_period == "first_half" else "Second Half"
            )

        leave_application.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.local.response = {}
        generate_error_log("debug", message=frappe.local.response)

        # Send a confirmation message to the user
        slack.slack_app.client.chat_postMessage(
            channel=user_info["id"],
            text=f"Your leave application has been submitted successfully. Application ID: {leave_application.name}",
        )

    except Exception as e:
        generate_error_log(
            "Slack Leave Application Error",
            message="Error Submitting request",
            exception=e,
        )


def half_day_checkbox_handler(slack: SlackIntegration, payload: dict):
    """
    Update the modal based on the half-day checkbox selection
    If half-day is selected, add the half-day date and period fields
    Otherwise, half-day is not selected, remove the related fields
    """
    # Extract view data from the Slack payload
    view = payload["view"]
    blocks = view["blocks"]

    # If half day is selected, add the blocks, otherwise remove them
    state = view["state"]["values"]
    # Get the start and end dates
    start_date = state["start_date"]["start_date_picker"]["selected_date"]
    end_date = state["end_date"]["end_date_picker"]["selected_date"]

    # Check if "Half Day" is selected in the checkboxes
    half_day_selected = (
        len(state["half_day_checkbox"]["half_day_checkbox"]["selected_options"]) > 0
    )

    # Check if start and end dates are the same
    same_day = start_date == end_date

    if half_day_selected:
        # If from_date and to_date are same, don't add this block
        if not same_day:
            blocks += [
                {
                    "type": "input",
                    "block_id": "half_day_date",
                    "element": {
                        "type": "datepicker",
                        "action_id": "half_day_date_picker",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select a date",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Half Day Date"},
                },
            ]

        blocks += [
            {
                "type": "input",
                "block_id": "half_day_period",
                "element": {
                    "type": "radio_buttons",
                    "action_id": "half_day_period_select",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "First Half",
                            },
                            "value": "first_half",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Second Half",
                            },
                            "value": "second_half",
                        },
                    ],
                },
                "label": {"type": "plain_text", "text": "Half Day Period"},
            },
        ]
    else:
        # Remove half-day-related blocks if they exist
        blocks = [
            block
            for block in blocks
            if block["block_id"] not in ["half_day_date", "half_day_period"]
        ]

    # Update the modal with the modified blocks
    updated_view = {
        "type": "modal",
        "callback_id": view["callback_id"],
        "title": view["title"],
        "submit": view["submit"],
        "blocks": blocks,
    }

    # Use views_update to update the modal
    slack.slack_app.client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=updated_view,
    )

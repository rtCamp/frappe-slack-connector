# Copyright (c) 2024, rtCamp and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from frappe.utils.jinja import validate_template

CELEBRATION_TEMPLATE_FIELDS = ("birthday_message_template", "anniversary_message_template")

# TODO: Add validation for slack and channel integration
# Currently we are taking the channel name (not the id), so it is
# not possible to get the conversations.info api that expects a channel
# id. One possible solution is to fetch all the paginated channels
# via conversations.list and then match the channel name with the provided channel name,
# and then get the channel id (and possibly store in document).
# This is a costly operation and should be avoided.


class SlackSettings(Document):
    def validate(self):
        """
        Check if the provided slack channel is valid, taking
        the slack_app_token and slack_bot_token from the document
        """
        self.validate_celebration_templates()

    def validate_celebration_templates(self):
        """
        Reject a celebrations message template with a Jinja syntax error so
        the daily job does not fail at post time
        """
        for fieldname in CELEBRATION_TEMPLATE_FIELDS:
            validate_template(self.get(fieldname))

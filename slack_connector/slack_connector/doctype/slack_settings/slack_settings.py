# Copyright (c) 2024, rtCamp and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

# from slack_connector.slack.app import SlackIntegration


class SlackSettings(Document):
    # TODO: Implement the following methods
    def validate(self):
        """
        Check if the provided slack channel is valid, taking
        the slack_app_token and slack_bot_token from the document
        """
        pass

    def before_save(self):
        """
        Update the slack settings in SlackIntegration class if the document is saved,
        reset the slack_app instance
        """
        pass

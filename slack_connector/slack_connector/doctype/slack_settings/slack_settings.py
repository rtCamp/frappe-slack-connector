# Copyright (c) 2024, rtCamp and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

from slack_connector.slack.app import SlackIntegration


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
        pass

    def before_save(self):
        """
        Update the slack settings in SlackIntegration class
        if the document is saved in runtime, reset the slack_app instance
        """
        SlackIntegration.SLACK_APP_TOKEN = self.slack_app_token
        SlackIntegration.SLACK_BOT_TOKEN = self.slack_bot_token
        SlackIntegration.SLACK_CHANNEL_ID = self.attendance_channel_id

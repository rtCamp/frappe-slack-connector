# Copyright (c) 2024, rtCamp and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, getdate, nowdate
from frappe.utils.jinja import validate_template

CELEBRATION_TEMPLATE_FIELDS = ("birthday_message_template", "anniversary_message_template")
CELEBRATION_TOGGLE_FIELDS = ("send_birthday_updates", "send_anniversary_updates")

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
        if not self.reset_celebrations_date_on_enable():
            self.keep_latest_celebrations_date()

    def reset_celebrations_date_on_enable(self) -> bool:
        """
        When birthday or anniversary updates are switched on, pretend the
        job last ran yesterday so the first run announces today's events
        (plus the preceding non-working days) instead of catching up a
        backlog from whenever the feature was last on.
        Returns True when the date was reset.
        """
        before = self.get_doc_before_save()
        for fieldname in CELEBRATION_TOGGLE_FIELDS:
            was_on = before.get(fieldname) if before else 0
            if self.get(fieldname) and not was_on:
                self.last_celebrations_date = add_days(nowdate(), -1)
                return True
        return False

    def keep_latest_celebrations_date(self):
        """
        The scheduler stamps last_celebrations_date directly in the DB, so a
        Desk form left open across a post would write the older date it
        loaded back and the next run would catch up (re-post) that day.
        Keep whichever of the stored and incoming dates is later.
        """
        stored = frappe.db.get_single_value("Slack Settings", "last_celebrations_date")
        incoming = self.get("last_celebrations_date")
        if stored and (not incoming or getdate(stored) > getdate(incoming)):
            self.last_celebrations_date = stored

    def validate_celebration_templates(self):
        """
        Reject a celebrations message template with a Jinja syntax error so
        the daily job does not fail at post time
        """
        for fieldname in CELEBRATION_TEMPLATE_FIELDS:
            validate_template(self.get(fieldname))

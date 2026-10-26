# Copyright (c) 2024, rtCamp and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.jinja import validate_template

from frappe_slack_connector.tasks.celebrations import exception_summary, render_slack_template, template_output_problem

# Sample template context per field, used to dry-render templates on save.
# The birthday context has no `years`, matching what the job passes.
CELEBRATION_TEMPLATE_SAMPLES = {
    "birthday_message_template": {"name": "Sample Employee", "mention": "Sample Employee", "company": "Sample Co"},
    "anniversary_message_template": {
        "name": "Sample Employee",
        "mention": "Sample Employee",
        "company": "Sample Co",
        "years": 1,
    },
}

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
        When a celebrations Email Template link changes, reject a template
        that does not exist, does not use HTML (its rich-text response would
        post raw HTML to Slack), has a Jinja syntax error, references a
        value that is not in the context (templates render with
        StrictUndefined, so {{ e.nmae }} and {% if e.nmae %} both fail
        here rather than post a placeholder or silently skip), fails to
        render for any other reason, or renders to nothing.

        Only a changed link is checked: background jobs save Slack Settings
        to write their stamps, and an Email Template edited after it was
        linked must not make those saves fail. The daily job re-checks the
        template when it renders and falls back to the default message.
        """
        for fieldname, sample in CELEBRATION_TEMPLATE_SAMPLES.items():
            template_name = self.get(fieldname)
            if not template_name or not self.has_value_changed(fieldname):
                continue
            label = _(frappe.unscrub(fieldname))
            template = frappe.db.get_value("Email Template", template_name, ["use_html", "response_html"], as_dict=True)
            if not template:
                frappe.throw(_("{0}: Email Template {1} does not exist").format(label, template_name))
            if not template.use_html:
                frappe.throw(
                    _(
                        "{0}: enable Use HTML on Email Template {1} and put the Slack message in Response (HTML), "
                        "so it is stored as plain Jinja and not as rich text"
                    ).format(label, template_name)
                )
            source = template.response_html or ""
            validate_template(source)
            try:
                rendered = render_slack_template(source, {"employees": [sample]})
            except Exception as e:
                frappe.throw(_("{0} could not be rendered: {1}").format(label, exception_summary(e)))
            problem = template_output_problem(rendered, label)
            if problem:
                frappe.throw(problem)

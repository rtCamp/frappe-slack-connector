import json

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.frappe_slack_connector.doctype.slack_settings.slack_settings import SlackSettings


class TestSlackSettingsTemplateValidation(IntegrationTestCase):
    def _template(self, source, *, use_html=1, name="_Test FSC Celebration Template"):
        """Create (or replace) a real Email Template holding ``source`` and return its name; removed after the test.

        Email Template validates its own Jinja on save, so the insert skips validation: these tests check
        that Slack Settings rejects a bad template on its own, whichever way it got into the database.
        """
        if frappe.db.exists("Email Template", name):
            frappe.delete_doc("Email Template", name, force=True)
        doc = frappe.get_doc(
            {
                "doctype": "Email Template",
                "__newname": name,
                "subject": "celebrations",
                "use_html": use_html,
                "response_html": source if use_html else None,
                "response": None if use_html else source,
            }
        )
        doc.flags.ignore_validate = True
        doc.insert()
        self.addCleanup(frappe.delete_doc, "Email Template", doc.name, force=True)
        return doc.name

    def _settings(self, *, birthday=None, anniversary=None, changed=True):
        """A stand-in Slack Settings doc; ``changed`` is what has_value_changed reports for the link fields."""
        return frappe._dict(
            birthday_message_template=birthday,
            anniversary_message_template=anniversary,
            has_value_changed=lambda fieldname: changed,
        )

    def test_template_fields_link_to_email_template(self):
        """Both template fields are Links to Email Template in the doctype JSON on disk, like the timesheet reminder template."""
        path = frappe.get_app_path(
            "frappe_slack_connector", "frappe_slack_connector", "doctype", "slack_settings", "slack_settings.json"
        )
        with open(path) as f:
            fields = {field["fieldname"]: field for field in json.load(f)["fields"]}
        for fieldname in ("birthday_message_template", "anniversary_message_template", "reminder_template"):
            self.assertIn(fieldname, fields)
            self.assertEqual(fields[fieldname]["fieldtype"], "Link", fieldname)
            self.assertEqual(fields[fieldname]["options"], "Email Template", fieldname)

    def test_rejects_missing_email_template(self):
        """A link to an Email Template that does not exist is rejected."""
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday="_Test FSC Nope"))

    def test_rejects_email_template_without_use_html(self):
        """An Email Template whose message lives in the rich-text Response (Use HTML off) is rejected: it would post raw HTML."""
        name = self._template("Hi {{ employees | length }}", use_html=0)
        with self.assertRaises(frappe.ValidationError) as ctx:
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))
        self.assertIn("Use HTML", str(ctx.exception))

    def test_rejects_template_with_jinja_syntax_error(self):
        """validate_celebration_templates raises a ValidationError for a syntactically invalid template."""
        name = self._template("{% if %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))

    def test_real_document_validate_rejects_bad_template(self):
        """doc.validate() on the actual Slack Settings document raises for a bad anniversary template."""
        name = self._template("{{ employees")
        doc = frappe.get_single("Slack Settings")
        doc.anniversary_message_template = name
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_skips_validation_when_link_is_unchanged(self):
        """A link that did not change in this save is not validated, so an Email Template edited after linking cannot make a background job's Slack Settings save fail."""
        name = self._template("{% if %}")
        SlackSettings.validate_celebration_templates(self._settings(birthday=name, anniversary=name, changed=False))

    def test_real_document_save_with_unchanged_bad_link_passes(self):
        """doc.validate() on the actual Slack Settings document passes when the (bad) link is the same as before the save."""
        name = self._template("{{ employees")
        doc = frappe.get_single("Slack Settings")
        doc.anniversary_message_template = name
        before = frappe.get_doc("Slack Settings")
        before.anniversary_message_template = name
        doc._doc_before_save = before
        doc.validate()

    def test_unknown_value_message_names_the_value(self):
        """The validation message for a typo names the missing attribute so the user can find it."""
        name = self._template("Hi {% for e in employees %}{{ e.nmae }}{% endfor %}")
        with self.assertRaises(frappe.ValidationError) as ctx:
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))
        self.assertIn("nmae", str(ctx.exception))

    def test_rejects_unknown_value_used_only_in_a_condition(self):
        """A typo used only in {% if %} renders non-empty output under DebugUndefined; StrictUndefined makes it an error here too."""
        name = self._template("Hi {% for e in employees %}{{ e.name }}{% if e.nmae %}!{% endif %}{% endfor %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))

    def test_accepts_valid_and_empty_templates(self):
        """A valid Email Template and an empty link pass validation."""
        name = self._template("Hi {{ employees | length }}")
        SlackSettings.validate_celebration_templates(self._settings(birthday=name, anniversary=""))

    def test_rejects_template_that_fails_to_render(self):
        """A template that is valid Jinja but fails at render time (e.g. uses a dunder attribute) is rejected on save."""
        name = self._template("{{ employees.__class__ }}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))

    def test_rejects_template_that_renders_empty(self):
        """A template that renders to nothing with a sample employee is rejected on save."""
        name = self._template("{% if false %}x{% endif %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(anniversary=name))

    def test_accepts_template_ending_in_file_extension(self):
        """A one-line template ending in .txt is rendered as text, not looked up as a template file."""
        name = self._template("Hi {{ employees[0].name }}, see notes.txt")
        SlackSettings.validate_celebration_templates(self._settings(birthday=name))

    def test_rejects_template_referencing_unknown_value(self):
        """A template with a typo such as {{ e.nmae }} is rejected, since it would post the literal placeholder every day."""
        name = self._template("Hi {% for e in employees %}{{ e.nmae }}{% endfor %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))

    def test_rejects_birthday_template_using_years(self):
        """years is not in the birthday context, so a birthday template using {{ e.years }} is rejected; the same template is fine for anniversaries."""
        name = self._template("{% for e in employees %}{{ e.name }} - {{ e.years }}{% endfor %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(self._settings(birthday=name))
        SlackSettings.validate_celebration_templates(self._settings(anniversary=name))

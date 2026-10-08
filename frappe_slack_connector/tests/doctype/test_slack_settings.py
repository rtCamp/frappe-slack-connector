import json

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.frappe_slack_connector.doctype.slack_settings.slack_settings import SlackSettings


class TestSlackSettingsTemplateValidation(IntegrationTestCase):
    def test_template_fields_are_code_fields(self):
        """Both template fields are Code (Jinja) fields in the doctype JSON on disk, which are exempt from the HTML sanitizer that would break Jinja."""
        path = frappe.get_app_path(
            "frappe_slack_connector", "frappe_slack_connector", "doctype", "slack_settings", "slack_settings.json"
        )
        with open(path) as f:
            fields = {field["fieldname"]: field for field in json.load(f)["fields"]}
        for fieldname in ("birthday_message_template", "anniversary_message_template"):
            self.assertIn(fieldname, fields)
            self.assertEqual(fields[fieldname]["fieldtype"], "Code", fieldname)
            self.assertEqual(fields[fieldname]["options"], "Jinja", fieldname)

    def test_rejects_template_with_jinja_syntax_error(self):
        """validate_celebration_templates raises a ValidationError for a syntactically invalid template."""
        doc = frappe._dict(birthday_message_template="{% if %}", anniversary_message_template=None)
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(doc)

    def test_real_document_validate_rejects_bad_template(self):
        """doc.validate() on the actual Slack Settings document raises for a bad anniversary template."""
        doc = frappe.get_single("Slack Settings")
        doc.anniversary_message_template = "{{ employees"
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_accepts_valid_and_empty_templates(self):
        """Valid Jinja and empty templates pass validation."""
        doc = frappe._dict(birthday_message_template="Hi {{ employees | length }}", anniversary_message_template="")
        SlackSettings.validate_celebration_templates(doc)

    def test_rejects_template_that_fails_to_render(self):
        """A template that is valid Jinja but fails at render time (e.g. uses a dunder attribute) is rejected on save."""
        doc = frappe._dict(birthday_message_template="{{ employees.__class__ }}", anniversary_message_template=None)
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(doc)

    def test_rejects_template_that_renders_empty(self):
        """A template that renders to nothing with a sample employee is rejected on save."""
        doc = frappe._dict(birthday_message_template=None, anniversary_message_template="{% if false %}x{% endif %}")
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(doc)

    def test_accepts_template_ending_in_file_extension(self):
        """A one-line template ending in .txt is rendered as text, not looked up as a template file."""
        doc = frappe._dict(
            birthday_message_template="Hi {{ employees[0].name }}, see notes.txt", anniversary_message_template=""
        )
        SlackSettings.validate_celebration_templates(doc)

    def test_rejects_template_referencing_unknown_value(self):
        """A template with a typo such as {{ e.nmae }} is rejected, since it would post the literal placeholder every day."""
        doc = frappe._dict(
            birthday_message_template="Hi {% for e in employees %}{{ e.nmae }}{% endfor %}",
            anniversary_message_template="",
        )
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(doc)

    def test_rejects_birthday_template_using_years(self):
        """years is not in the birthday context, so a birthday template using {{ e.years }} is rejected; the same template is fine for anniversaries."""
        template = "{% for e in employees %}{{ e.name }} - {{ e.years }}{% endfor %}"
        with self.assertRaises(frappe.ValidationError):
            SlackSettings.validate_celebration_templates(
                frappe._dict(birthday_message_template=template, anniversary_message_template="")
            )
        SlackSettings.validate_celebration_templates(
            frappe._dict(birthday_message_template="", anniversary_message_template=template)
        )

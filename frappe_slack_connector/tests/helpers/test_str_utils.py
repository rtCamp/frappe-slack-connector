from frappe.tests import IntegrationTestCase

from frappe_slack_connector.helpers.str_utils import escape_slack_text


class TestEscapeSlackText(IntegrationTestCase):
    def test_escapes_ampersand_and_angle_brackets(self):
        """escape_slack_text replaces &, < and > with their Slack entities, ampersand first so it is not double-escaped."""
        self.assertEqual(escape_slack_text("Tom & <Jerry>"), "Tom &amp; &lt;Jerry&gt;")

    def test_returns_empty_string_for_none(self):
        """escape_slack_text returns '' for None or empty input."""
        self.assertEqual(escape_slack_text(None), "")
        self.assertEqual(escape_slack_text(""), "")

    def test_leaves_plain_text_untouched(self):
        """Text without control characters is returned unchanged."""
        self.assertEqual(escape_slack_text("Alice Example"), "Alice Example")

import re

from frappe_slack_connector.slack.app import SlackIntegration


def strip_html_tags(text):
    """
    Remove HTML tags from the given text
    """
    # Regex pattern to match HTML tags
    pattern = re.compile("<.*?>")

    # Use sub() to replace anything that matches the pattern with an empty string
    clean_text = re.sub(pattern, "", text)

    return clean_text


def truncate_text(text, limit=SlackIntegration.SLACK_CHAR_LIMIT):
    """
    Truncate the text to the given limit
    """
    return text[:limit]

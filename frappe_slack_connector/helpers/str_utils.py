import re


def strip_html_tags(text):
    """
    Remove HTML tags from the given text
    """
    # Regex pattern to match HTML tags
    pattern = re.compile("<.*?>")

    # Use sub() to replace anything that matches the pattern with an empty string
    clean_text = re.sub(pattern, "", text)

    return clean_text

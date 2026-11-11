from frappe_slack_connector.setup import install


def after_install() -> None:
    """Create the app's custom fields and property setters when it is installed on a site."""
    install()

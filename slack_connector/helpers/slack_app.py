import frappe
from slack_bolt import App

SLACK_BOT_TOKEN = None
SLACK_APP_TOKEN = None
SLACK_CHANNEL_ID = None

slack_app = None


def _setup_slack_app() -> None:
    global slack_app, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID
    settings = frappe.get_single("Slack Settings")
    SLACK_BOT_TOKEN = settings.get_password("slack_bot_token")
    SLACK_APP_TOKEN = settings.get_password("slack_app_token")
    SLACK_CHANNEL_ID = settings.get_password("attendance_channel_id")
    slack_app = App(token=SLACK_BOT_TOKEN)


_setup_slack_app()

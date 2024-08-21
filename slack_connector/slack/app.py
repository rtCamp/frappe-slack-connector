import frappe
from slack_bolt import App

from slack_connector.db.user_meta import get_user_meta


class SlackIntegration:
    SLACK_BOT_TOKEN = None
    SLACK_APP_TOKEN = None
    SLACK_CHANNEL_ID = None

    def __init__(self):
        self.slack_app = self.__setup_slack_app()

    @classmethod
    def __check_slack_config(cls) -> bool:
        """
        Check if the Slack configuration is set up
        """
        return all(
                getattr(cls, slack_attr) is not None
                for slack_attr in cls.__dict__.keys()
                if slack_attr.startswith("SLACK_")
        )

    @classmethod
    def __setup_slack_app(cls) -> App:
        """
        Store the configuration in the class and return the Slack App instance
        """
        if not cls.__check_slack_config():
            settings = frappe.get_single("Slack Settings")
            cls.SLACK_BOT_TOKEN = settings.get_password("slack_bot_token")
            cls.SLACK_APP_TOKEN = settings.get_password("slack_app_token")
            cls.SLACK_CHANNEL_ID = settings.get_password("attendance_channel_id")
            # Still not set, raise an error
            if not cls.__check_slack_config():
                frappe.log_error("Slack Config not set in the Slack Settings")
                frappe.throw("Slack Config not set in the Slack Settings")
        return App(token=cls.SLACK_BOT_TOKEN)

    def get_slack_users(self, limit: int = 1000) -> dict:
        """
        Get all users from Slack and return a dictionary of email and username
        """
        # TODO: Make this paginated for bigger payloads
        result = self.slack_app.client.users_list(limit=limit)
        users = result.get("members", {})

        user_dict = {}
        for user in users:
            email = user.get("profile", {}).get("email")
            if (
                email is None
                or user["deleted"]
                or user["is_bot"]
                or user["is_app_user"]
            ):
                continue
            user_dict[email] = {
                "id": user["id"],
                "name": user["name"],
                "real_name": user["real_name"],
            }
        return user_dict

    def get_slack_user(
        self,
        user_email: str = None,
        employee_id: str = None,
        check_meta: bool = True,
        from_api: bool = False,
    ) -> dict | None:
        """
        Get the Slack user for the given user email
        NOTE: First checks the User Meta for slack details, if not found, fetches from Slack
        Optionally, fetches directly from Slack if `from_api` is True
        """
        if not user_email and not employee_id:
            raise ValueError("Either user_email or employee_id is required")
        if user_email and employee_id:
            raise ValueError("Only one of user_email or employee_id is required")

        # If from API, directly fetch from Slack
        if from_api:
            if employee_id:
                email = get_user_meta(employee_id=employee_id).user
            else:
                email = user_email
            return self.slack_app.client.users_lookupByEmail(email=email)

        # If not from user meta but employee_id is provided, fetch user email first
        if not check_meta and employee_id:
            user_email = get_user_meta(employee_id=employee_id).user
            slack_user = self.slack_app.client.users_lookupByEmail(email=user_email)
            return {
                "id": slack_user.get("user", {}).get("id"),
                "name": slack_user.get("user", {}).get("name"),
            }

        user_meta = None
        if check_meta:
            user_meta = (
                get_user_meta(user_id=user_email)
                if user_email
                else get_user_meta(employee_id=employee_id)
            )
            if user_meta and user_meta.custom_username:
                return {
                    "id": user_meta.custom_username,
                    "name": user_meta.custom_slack_username,
                }
        slack_email = user_meta.user if user_meta else user_email
        if not slack_email:
            return None
        slack_user = self.slack_app.client.users_lookupByEmail(email=slack_email)
        return {
            "id": slack_user.get("user", {}).get("id"),
            "name": slack_user.get("user", {}).get("name"),
        }

    def get_slack_user_id(self, *args, **kwargs) -> str | None:
        """
        Get the Slack user ID for the given user email
        """
        slack_user = self.get_slack_user(*args, **kwargs)
        return slack_user.get("id") if slack_user else None

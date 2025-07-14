import frappe
from frappe import _
from slack_bolt import App

from frappe_slack_connector.db.user_meta import get_user_meta
from frappe_slack_connector.helpers.error import generate_error_log

####################################################################
#                                                                  #
# Slack Integration                                                #
# -----------------------------------------------------------------#
# This class is used to interact with the Slack API                #
# and a few helper functions                                       #
#                                                                  #
####################################################################


class SlackIntegration:
    SLACK_CHAR_LIMIT = 75

    def __init__(self):
        """
        Initialize the Slack Integration instance
        """
        settings = frappe.get_single("Slack Settings")
        self.SLACK_BOT_TOKEN = settings.get_password("slack_bot_token")
        self.SLACK_APP_TOKEN = settings.get_password("slack_app_token")
        self.SLACK_CHANNEL_ID = settings.get_password("attendance_channel_id")
        self.SLACK_SIGNATURE = settings.get_password("slack_signing_token")

        # Still not set, raise an error
        if not self.__check_slack_config():
            generate_error_log(
                title="Slack Config not set in the Slack Settings",
                msgprint=True,
            )
        self.slack_app = App(token=self.SLACK_BOT_TOKEN)

    def __check_slack_config(self) -> bool:
        """
        Check if the Slack configuration is set up
        """
        return all(
            getattr(self, slack_attr) is not None
            for slack_attr in self.__dict__.keys()
            if slack_attr.startswith("SLACK_")
        )

    def get_slack_users(self, limit: int = 500) -> dict:
        """
        Get all users from Slack and return a dictionary of email and username
        This function uses pagination to handle large numbers of users
        """
        user_dict = {}
        cursor = None

        while True:
            try:
                # Make API call with cursor if it exists
                if cursor:
                    result = self.slack_app.client.users_list(limit=limit, cursor=cursor)
                else:
                    result = self.slack_app.client.users_list(limit=limit)

                users = result.get("members", [])

                for user in users:
                    email = user.get("profile", {}).get("email")
                    if email is None or user["deleted"] or user["is_bot"] or user["is_app_user"]:
                        continue
                    user_dict[email] = {
                        "id": user["id"],
                        "name": user["name"],
                        "real_name": user["real_name"],
                    }

                # Check if there are more users to fetch
                cursor = result.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break  # No more users to fetch

            except Exception as e:
                generate_error_log(
                    title="Error fetching Slack users",
                    exception=e,
                )
                break  # Exit the loop if there's an error

        return user_dict

    def get_slack_user(
        self,
        user_email: str | None = None,
        employee_id: str | None = None,
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

        try:
            # If from API, directly fetch from Slack
            if from_api:
                if employee_id:
                    # FIXME: Possible NoneType Error
                    email = get_user_meta(employee_id=employee_id).user
                else:
                    email = user_email
                return self.slack_app.client.users_lookupByEmail(email=email)

            # If not from user meta but employee_id is provided, fetch user email first
            if not check_meta and employee_id:
                # FIXME: Possible NoneType Error
                user_email = get_user_meta(employee_id=employee_id).user
                slack_user = self.slack_app.client.users_lookupByEmail(email=user_email)
                return {
                    "id": slack_user.get("user", {}).get("id"),
                    "name": slack_user.get("user", {}).get("name"),
                }

            user_meta = None
            if check_meta:
                user_meta = get_user_meta(user_id=user_email) if user_email else get_user_meta(employee_id=employee_id)
                if user_meta and user_meta.custom_slack_userid:
                    return {
                        "id": user_meta.custom_slack_userid,
                        "name": user_meta.custom_slack_username,
                    }
            slack_email = user_meta.user if user_meta else user_email
            if not slack_email:
                return None

            try:
                slack_user = self.slack_app.client.users_lookupByEmail(email=slack_email)
            except Exception as e:
                generate_error_log(
                    title="Error fetching Slack user",
                    message=f"User Email: {slack_email}",
                    exception=e,
                )
                return None

            return {
                "id": slack_user.get("user", {}).get("id"),
                "name": slack_user.get("user", {}).get("name"),
            }
        except Exception as e:
            generate_error_log(
                title="Error fetching Slack user",
                message="Please check the user email or employee ID and try again.",
                exception=e,
            )
            return None

    def verify_slack_request(
        self,
        signature: str,
        timestamp: str,
        req_data: str,
    ) -> None:
        import hashlib
        import hmac
        import time

        # Verify the timestamp to prevent replay attacks
        if abs(time.time() - int(timestamp)) > 60 * 5:
            generate_error_log(
                title="Slack Timestamp verification failed",
                msgprint=_("Request is too old"),
            )
            frappe.throw(_("Request is too old"), frappe.PermissionError)

        # Create the signature base string
        sig_basestring = f"v0:{timestamp}:{req_data}"

        # Compute the hash using your Slack signing secret
        request_signature = (
            "v0=" + hmac.new(self.SLACK_SIGNATURE.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
        )

        # Compare the computed signature with the received signature
        if not hmac.compare_digest(request_signature, signature):
            generate_error_log(title="Slack Signature verification failed", message="Slack Event")
            frappe.throw(_("Invalid request signature"), frappe.PermissionError)

    def get_slack_user_id(self, *args, **kwargs) -> str | None:
        """
        Get the Slack user ID for the given user email
        """
        slack_user = self.get_slack_user(*args, **kwargs)
        return slack_user.get("id") if slack_user else None

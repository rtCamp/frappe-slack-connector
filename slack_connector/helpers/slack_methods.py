import frappe

from slack_connector.helpers.slack_app import slack_app
from slack_connector.helpers.user_meta import update_user_meta


def get_employee_company_email(user_email: str = None):
    # If no user is provided, get the current user
    if not user_email:
        user_email = frappe.session.user_email

    try:
        # Find the Employee record for the user
        employee = frappe.get_all(
            "Employee",
            filters={
                "status": "Active",
            },
            or_filters={
                "user_id": user_email,
                "company_email": user_email,
                "personal_email": user_email,
            },
            fields=["name", "company_email"],
            limit=1,
        )

        if employee:
            # If an Employee record is found, return the company_email
            return employee[0].company_email
        else:
            frappe.log_error(f"No Employee record found for user {user_email}")
            return None

    except Exception as e:
        frappe.log_error(f"Error fetching employee company email: {str(e)}")
        return None


def map_users_to_employee(user_dict: dict) -> None:
    """
    Map Slack users to Employee records
    """
    for email, slack_id in user_dict.items():
        update_user_meta(
            {
                "custom_username": slack_id,
            },
            user=email,
            upsert=False,
        )


def get_slack_users() -> dict:
    """
    Get all users from Slack and return a dictionary of email and username
    """
    # FIXME: Make this paginated for bigger payloads
    result = slack_app.client.users_list(limit=1000)
    users = result.get("members", {})

    user_dict = {}
    for user in users:
        email = user.get("profile", {}).get("email")
        if email is None or user["deleted"] or user["is_bot"] or user["is_app_user"]:
            continue
        user_dict[email] = user["id"]
    return user_dict


def get_slack_user_id(user_email: str) -> str:
    """
    Get the Slack user ID for the given user email
    """
    slack_user = slack_app.client.users_lookupByEmail(email=user_email)
    return slack_user["user"]["id"]

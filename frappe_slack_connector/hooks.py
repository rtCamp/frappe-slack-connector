app_name = "frappe_slack_connector"
app_title = "Frappe Slack Connector"
app_publisher = "rtCamp"
app_description = "The app is used to integrate Slack into Frappe site"
app_email = "erp@rtcamp.com"
app_license = "GNU AFFERO GENERAL PUBLIC LICENSE (v3)"
source_link = "https://github.com/rtCamp/frappe-slack-connector"
required_apps = ["frappe/erpnext", "frappe/hrms"]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/frappe_slack_connector/css/frappe_slack_connector.css"
# app_include_js = "/assets/frappe_slack_connector/js/frappe_slack_connector.js"

# include js, css files in header of web template
# web_include_css = "/assets/frappe_slack_connector/css/frappe_slack_connector.css"
# web_include_js = "/assets/frappe_slack_connector/js/frappe_slack_connector.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "frappe_slack_connector/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
    "User Meta": "public/js/user_meta.js",
    "User": "public/js/user_doctype.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Fixtures
# ----------
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            [
                "module",
                "in",
                ["Frappe Slack Connector"],
            ]
        ],
    },
]


# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "frappe_slack_connector/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "frappe_slack_connector.utils.jinja_methods",
# 	"filters": "frappe_slack_connector.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "frappe_slack_connector.install.before_install"
# after_install = "frappe_slack_connector.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "frappe_slack_connector.uninstall.before_uninstall"
# after_uninstall = "frappe_slack_connector.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "frappe_slack_connector.utils.before_app_install"
# after_app_install = "frappe_slack_connector.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "frappe_slack_connector.utils.before_app_uninstall"
# after_app_uninstall = "frappe_slack_connector.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "frappe_slack_connector.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
    "Leave Application": {
        "after_insert": "frappe_slack_connector.override.leave_application.after_insert",
    },
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "all": ["frappe_slack_connector.tasks.attendance_summary.attendance_channel"],
    "daily": ["frappe_slack_connector.tasks.send_daily_reminder.send_reminder"],
    # "hourly": ["frappe_slack_connector.tasks.hourly"],
    # "weekly": ["frappe_slack_connector.tasks.weekly"],
    # "monthly": ["frappe_slack_connector.tasks.monthly"],
}

# Testing
# -------

# before_tests = "frappe_slack_connector.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "frappe_slack_connector.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "frappe_slack_connector.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["frappe_slack_connector.utils.before_request"]
# after_request = ["frappe_slack_connector.utils.after_request"]

# Job Events
# ----------
# before_job = ["frappe_slack_connector.utils.before_job"]
# after_job = ["frappe_slack_connector.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"frappe_slack_connector.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

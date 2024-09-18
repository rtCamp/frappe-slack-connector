# Frappe Slack Integration

This Frappe app integrates the Leave Management module with Slack, allowing seamless leave application and approval workflows directly within Slack.

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)

## Features

- **Leave Application via Slack**: Users can apply for leave directly from Slack without accessing the Frappe interface.
- **Approval Notifications**: Leave approvers will receive a notification in Slack to either approve or reject the leave requests.
- **Daily Leave Reminder**: Every working day, at a specified time, a reminder is sent listing the employees who are on leave for that day in the attendance channel.

## Installation

To install the app:

1. Clone the repository into your Frappe project:
   ```bash
   cd ~/frappe-bench/apps
   git clone https://github.com/rtCamp/frappe-slack-connector
   ```

2. Install the app:
   ```bash
   cd ~/frappe-bench
   bench --site [site-name] install-app frappe-slack-connector
   ```

3. Migrate to apply database changes:
   ```bash
   bench --site [site-name] migrate
   ```

## Configuration

1. **Slack API Setup**:
   - Set up a Slack app and get the API token.
   - Add the API token to your site's configuration in Frappe.

2. **Leave Reminder Setup**:
   - Configure the daily reminder time for employees on leave via the app's settings.

3. **Approvers Notification**:
   - Ensure the approvers are defined correctly in the Frappe Leave Management module, and notifications will automatically be sent when there are pending leave requests.

For detailed steps, please refer to our [wiki](https://github.com/rtCamp/frappe-slack-connector/wiki).

## Usage

- **Apply for Leave**: Use a custom Slack command to apply for leave. For example:
   ```
   /apply-leave
   ```
   This will open up a modal that you can use to submit your leave details.

- **Leave Approval/ Rejection**: Leave approvers will receive a Slack notification with options to either Approve or Reject the leave directly from Slack.

- **Daily Leave Reminders**: Every working day at the specified time, a Slack message will list all employees currently on leave.


## Documentation

Refer to our [Wiki](https://github.com/rtCamp/frappe-slack-connector/wiki)


## Contribution Guide

1. Install the apps with the help of the [Installation Guide](#installation).

2. Set up [pre-commit](https://pre-commit.com/) in the app.

```bash
cd frappe-slack-connector
pre-commit install
```

3. Push the code to the given branch.

```bash
cd frappe-slack-connector

git pull origin main # Make sure to pull the latest changes before making the PR

git checkout -b "new/branch"
git add --all
git commit -m ".."
git push origin "new/branch"
```

For branch names and commit messages, follow the guidelines at: https://www.conventionalcommits.org/en/v1.0.0/


4. Make the PR to the stage branch and then from stage to main.

## License

This project is licensed under the [MIT License](license.txt).

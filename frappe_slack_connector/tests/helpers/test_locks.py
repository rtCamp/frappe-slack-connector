from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from frappe_slack_connector.helpers.locks import job_lock

LOCKS_MODULE = "frappe_slack_connector.helpers.locks"


class TestJobLock(IntegrationTestCase):
    def test_commits_once_the_lock_is_held(self):
        """The commit restarts the transaction, so the job reads what the previous holder committed."""
        with patch(f"{LOCKS_MODULE}.frappe.db.commit") as commit:
            with job_lock("fsc_test_lock", timeout=5):
                commit.assert_called_once()

    def test_same_name_can_be_taken_again_after_release(self):
        with patch(f"{LOCKS_MODULE}.frappe.db.commit"):
            with job_lock("fsc_test_lock", timeout=5):
                pass
            with job_lock("fsc_test_lock", timeout=5):
                pass

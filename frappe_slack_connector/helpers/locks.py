from contextlib import contextmanager

import frappe
from frappe.utils.synchronization import filelock


@contextmanager
def job_lock(name: str, timeout: int):
    """
    Hold a lock for a background job and restart its transaction once held: under REPEATABLE READ it would
    otherwise keep reading the rows as they were before the wait, not what the previous holder committed
    """
    with filelock(name, timeout=timeout):
        frappe.db.commit()  # nosemgrep
        yield

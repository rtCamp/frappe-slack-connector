import importlib
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

import frappe_slack_connector
from frappe_slack_connector import hooks

APP = "frappe_slack_connector"
# A dotted path into this app, as hooks.py writes them. Bundle names such as
# "frappe_slack_connector.bundle.js" (app_include_js) look the same but are
# assets, not Python
DOTTED_PATH = re.compile(rf"^{APP}\.[\w.]+$")
ASSET_SUFFIXES = (".js", ".css", ".html")
# Top-level directories that hold no app code
SKIPPED_DIRS = {"tests", "public", "node_modules", "__pycache__"}


def app_modules() -> list[str]:
    """
    Every module in the app, found on disk: several directories (tasks/,
    slack/interactions/) have no __init__.py and pkgutil skips those. The
    tests directory and the per-doctype ``test_*.py`` files are left out
    """
    root = Path(frappe_slack_connector.__file__).parent
    names = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[0] in SKIPPED_DIRS or parts[-1].startswith("test_"):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join((APP, *parts)))
    return names


def _dotted_paths(value) -> set[str]:
    """Collect every app dotted path found in a hooks.py value (str, list, dict, nested)."""
    if isinstance(value, str):
        return {value} if DOTTED_PATH.match(value) and not value.endswith(ASSET_SUFFIXES) else set()
    if isinstance(value, dict):
        return set().union(*(_dotted_paths(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple, set)):
        return set().union(*(_dotted_paths(v) for v in value)) if value else set()
    return set()


def _hook_paths() -> set[str]:
    return set().union(*(_dotted_paths(getattr(hooks, name)) for name in dir(hooks) if not name.startswith("_")))


class TestImports(IntegrationTestCase):
    """
    Import every module and resolve every hooks.py entry point.

    Frappe only imports a module when something first needs it, so a helper
    that an upstream app renamed (hrms dropped a re-export in 16.20.0 and the
    Slack interactions endpoint answered HTTP 417 until the connector caught up)
    is invisible to tests that never touch that module. Running against the
    upstream branches this app targets, this test fails the moment a dependency
    moves underneath an entry point.
    """

    def test_every_module_imports(self):
        failures = {}
        for name in app_modules():
            try:
                importlib.import_module(name)
            except Exception as e:
                failures[name] = f"{type(e).__name__}: {e}"
        self.assertEqual(failures, {}, f"modules that no longer import: {failures}")

    def test_every_hook_entry_point_resolves(self):
        paths = _hook_paths()
        self.assertTrue(paths, "hooks.py should reference at least one app function")
        failures = {}
        for path in sorted(paths):
            try:
                frappe.get_attr(path)
            except Exception as e:
                failures[path] = f"{type(e).__name__}: {e}"
        self.assertEqual(failures, {}, f"hooks.py entries that no longer resolve: {failures}")

    def test_hook_paths_include_the_slack_entry_points_and_jobs(self):
        """Sanity check on the collector: the handlers that broke in production are covered."""
        paths = _hook_paths()
        self.assertIn(f"{APP}.tasks.send_daily_reminder.send_reminder", paths)
        self.assertIn(f"{APP}.override.leave_application.after_insert", paths)
        modules = app_modules()
        self.assertIn(f"{APP}.api.slack_interactions", modules)
        # Directories without __init__.py must still be walked
        self.assertIn(f"{APP}.slack.interactions.submit_leave", modules)
        self.assertIn(f"{APP}.tasks.send_daily_reminder", modules)
        self.assertFalse(
            [m for m in modules if m.startswith(f"{APP}.tests") or m.rsplit(".", 1)[-1].startswith("test_")]
        )

    def test_asset_names_are_not_taken_for_entry_points(self):
        """app_include_js = "frappe_slack_connector.bundle.js" must not be resolved with get_attr."""
        self.assertEqual(
            _dotted_paths([f"{APP}.bundle.js", f"{APP}.bundle.css", f"{APP}.api.health.slack_endpoints"]),
            {f"{APP}.api.health.slack_endpoints"},
        )

// Copyright (c) 2024, rtCamp and contributors
// For license information, please see license.txt

frappe.ui.form.on("Slack Settings", {
  refresh: function (frm) {
    if (!frm.is_new()) {
      frm.add_custom_button(__("Sync Slack Data"), () => {
        frm.events.sync_slack_data(frm);
      });
    }
  },
  sync_slack_data: function (frm) {
    frappe.call({
      method: "slack_connector.api.sync_slack_settings.sync_slack_data",
      callback: function (r) {
        if (r && !r.exc) {
          frappe.msgprint(__("Syncing Slack Details"));
          frm.refresh();
        }
      },
    });
  },
});
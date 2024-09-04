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
    });
  },

  test_slack_channel: function (frm) {
    frappe.call({
      method: "slack_connector.api.test_slack_channel.test_channel",
      args: { channel_id: frm.doc.attendance_channel_id },
      callback: function (response) {
        if (!response.exc) {
          frappe.msgprint({
            title: __("Success"),
            message: __(
              `Sent test message to the Slack channel: <strong>#${frm.doc.attendance_channel_id}</strong>`,
            ),
            indicator: "green",
          });
        } else {
          frappe.msgprint({
            title: __("Error"),
            message: __("Slack channel is invalid."),
            indicator: "red",
          });
        }
      },
    });
  },
});

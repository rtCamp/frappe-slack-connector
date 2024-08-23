// Copyright (c) 2024, rtCamp and contributors
// For license information, please see license.txt

frappe.ui.form.on("Slack Settings", {
  refresh: function (frm) {
    if (!frm.is_new()) {
      frm.add_custom_button(__("Sync Slack Data"), () => {
        frm.events.sync_slack_data(frm);
      });
      frm.add_custom_button(__("Send Attendance"), () => {
        frm.events.send_attendance(frm);
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

  test_slack_channel: function (frm) {
    frappe.call({
      method: "slack_connector.api.test_slack_channel.test_channel",
      args: { channel_id: frm.doc.attendance_channel_id },
      callback: function (response) {
        if (!response.exc) {
          frappe.msgprint({
            title: __("Success"),
            message: __(
              `Sent test message to the Slack channel: ${frm.doc.attendance_channel_id}`,
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

  send_attendance: function (frm) {
    frappe.call({
      method: "slack_connector.api.attendance_summary.attendance_channel",
      callback: function (response) {
        if (!response.exc) {
          frappe.msgprint({
            title: __("Success"),
            message: __(`Sent Attendance to #${frm.doc.attendance_channel_id}`),
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

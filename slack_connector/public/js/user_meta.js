frappe.ui.form.on("User Meta", {
  custom_fetch_slack_details: function (frm) {
    frappe.call({
      method: "slack_connector.api.auth.connect_slack",
      args: { user_email: frm.doc.name },
      callback: function (r) {
        if (r && !r.exc) {
          frappe.msgprint(__("Synced Slack Details"));
          frm.refresh();
        }
      },
    });
  },
});

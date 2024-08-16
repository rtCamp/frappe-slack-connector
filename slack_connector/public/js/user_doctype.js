frappe.ui.form.on("User", {
  refresh: function (frm) {
    frm.add_custom_button(__("Connect Slack"), function () {
      frm.events.connect_slack(frm);
    });
  },
  connect_slack: function (frm) {
    frappe.call({
      method: "slack_connector.api.auth.connect_slack",
      args: { user_email: frappe.session.user_email },
      callback: function (response) {
        if (!response.exc) {
          frm.save();
          frappe.msgprint({
            title: __("Success"),
            message: __("Slack connected successfully."),
            indicator: "green",
          });
        } else {
          frappe.msgprint({
            title: __("Error"),
            message: __("An error occurred while processing your request."),
            indicator: "red",
          });
        }
      },
    });
  },
});

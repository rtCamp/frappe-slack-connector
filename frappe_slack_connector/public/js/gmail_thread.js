/*
 * Gmail Thread form: send the thread to a Slack channel, see the state of
 * its mirror, stop it or retry it. Shown only while Gmail thread mirroring
 * is enabled in Slack Settings.
 */

const MIRROR_API = "frappe_slack_connector.gmail_mirror.api";
const SLACK_GROUP = __("Slack");

frappe.ui.form.on("Gmail Thread", {
  refresh: function (frm) {
    if (frm.is_new()) {
      return;
    }
    // A form can refresh again before the answer arrives: only the latest request draws
    const request = (frm.slack_mirror_request =
      (frm.slack_mirror_request || 0) + 1);
    frappe.call({
      method: `${MIRROR_API}.get_status`,
      args: { thread: frm.doc.name },
      callback: function (r) {
        if (
          request !== frm.slack_mirror_request ||
          !r.message ||
          !r.message.enabled
        ) {
          return;
        }
        show_mirror_state(frm, r.message.mirror);
      },
    });
  },
});

function show_mirror_state(frm, mirror) {
  // The banner takes HTML, and the channel name and error text are not ours
  const channel = mirror
    ? frappe.utils.escape_html(mirror.channel_name || "")
    : "";
  const error = mirror ? frappe.utils.escape_html(mirror.last_error || "") : "";

  if (!mirror || mirror.status === "Stopped") {
    if (mirror) {
      banner(
        frm,
        __("Mirroring to Slack channel #{0} was stopped.", [channel]),
        "yellow",
      );
    }
    frm.add_custom_button(
      __("Send to Slack"),
      () => send_to_slack(frm),
      SLACK_GROUP,
    );
    return;
  }

  if (mirror.status === "Failed") {
    banner(
      frm,
      __("Mirroring to Slack channel #{0} failed: {1}", [channel, error]),
      "red",
    );
    frm.add_custom_button(__("Retry"), () => retry_mirror(frm), SLACK_GROUP);
  } else if (mirror.retry_count > 0) {
    banner(
      frm,
      __(
        "Mirrored in Slack channel #{0}. The last attempt failed and will be retried: {1}",
        [channel, error],
      ),
      "orange",
    );
  } else {
    banner(frm, __("Mirrored in Slack channel #{0}.", [channel]), "blue");
  }
  frm.add_custom_button(
    __("Stop mirroring"),
    () => stop_mirror(frm, mirror.channel_name),
    SLACK_GROUP,
  );
}

function banner(frm, html, colour) {
  frm.dashboard.add_comment(html, colour, true);
}

function send_to_slack(frm) {
  frappe.prompt(
    [
      {
        fieldname: "channel",
        fieldtype: "Link",
        options: "Slack Channel",
        label: __("Slack channel"),
        reqd: 1,
      },
    ],
    function (values) {
      frappe.call({
        method: `${MIRROR_API}.start`,
        args: { thread: frm.doc.name, channel: values.channel },
        freeze: true,
        callback: function (r) {
          if (!r.exc) {
            frappe.show_alert({
              message: __("Sending this thread to #{0}", [values.channel]),
              indicator: "green",
            });
            frm.refresh();
          }
        },
      });
    },
    __("Send to Slack"),
    __("Send"),
  );
}

function stop_mirror(frm, channel_name) {
  frappe.confirm(
    __(
      "Stop mirroring this thread to #{0}? Messages already in Slack stay there.",
      [frappe.utils.escape_html(channel_name || "")],
    ),
    function () {
      frappe.call({
        method: `${MIRROR_API}.stop`,
        args: { thread: frm.doc.name },
        freeze: true,
        callback: function (r) {
          if (!r.exc) {
            frm.refresh();
          }
        },
      });
    },
  );
}

function retry_mirror(frm) {
  frappe.call({
    method: `${MIRROR_API}.retry`,
    args: { thread: frm.doc.name },
    freeze: true,
    callback: function (r) {
      if (!r.exc) {
        frappe.show_alert({ message: __("Retrying"), indicator: "green" });
        frm.refresh();
      }
    },
  });
}

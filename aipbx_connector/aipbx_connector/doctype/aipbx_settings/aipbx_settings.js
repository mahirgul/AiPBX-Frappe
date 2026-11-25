// AiPBX Settings: paste the connection pack, test it, fetch the extensions.
frappe.ui.form.on("AiPBX Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Paste connection pack"), () => {
			frappe.prompt(
				{ fieldname: "pack", label: __("Connection pack (JSON from AiPBX → Integration → Connections)"), fieldtype: "Code", reqd: 1 },
				(v) =>
					frappe.xcall("aipbx_connector.api.apply_pack", { pack: v.pack }).then(() => {
						frappe.show_alert({ message: __("Connected."), indicator: "green" });
						frm.reload_doc();
					}),
				__("Connect to AiPBX"),
				__("Connect")
			);
		});
		if (frm.doc.connection_uuid) {
			frm.add_custom_button(__("Test connection"), () =>
				frappe.xcall("aipbx_connector.api.test_connection").then((m) => frappe.msgprint(m))
			);
			frm.add_custom_button(__("Fetch extensions"), () =>
				frappe.xcall("aipbx_connector.api.sync_extensions").then((m) => frappe.msgprint(m))
			);
		}
	},
});

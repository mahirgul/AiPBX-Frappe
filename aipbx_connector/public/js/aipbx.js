// AiPBX click-to-call: the phone icon of every phone field calls through the PBX. The user's own
// extension rings first; when it is picked up, the PBX dials the number.
frappe.provide("frappe.phone_call");

if (frappe.boot && frappe.boot.aipbx && frappe.boot.aipbx.click_to_call) {
	frappe.phone_call.handler = (to_number, frm) => {
		const numbers = (Array.isArray(to_number) ? to_number : String(to_number || "").split("\n"))
			.map((n) => n.trim())
			.filter(Boolean);
		const call = (number) => {
			frappe
				.xcall("aipbx_connector.api.click_to_call", {
					to_number: number,
					link_doctype: frm ? frm.doctype : null,
					link_name: frm ? frm.docname : null,
				})
				.then((r) =>
					frappe.show_alert({
						message: __("Your phone ({0}) rings first, then {1} is dialled.", [r.extension, r.number]),
						indicator: "green",
					})
				);
		};
		if (numbers.length === 1) {
			call(numbers[0]);
			return;
		}
		frappe.prompt(
			{ fieldname: "number", label: __("Number"), fieldtype: "Select", options: numbers, default: numbers[0], reqd: 1 },
			(v) => call(v.number),
			__("Call through AiPBX"),
			__("Call")
		);
	};
}

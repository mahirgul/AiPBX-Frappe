"""Scheduled jobs: contacts to the PBX address book, and a retry of events that failed."""

import frappe

from aipbx_connector import api

BATCH = 500


def retry_events():
	"""Events that failed (a lock, a restart) run again, five times at most."""
	if not api.is_ready():
		return
	for name in frappe.get_all(
		"AiPBX Event", filters={"status": ["in", ["Received", "Failed"]], "attempts": ["<", 5]},
		pluck="name", order_by="creation asc", limit=50,
	):
		api.process_event(frappe.get_doc("AiPBX Event", name))


def _numbers(rows) -> list[dict]:
	out, seen = [], set()
	for label, number in rows:
		number = "".join(str(int(c)) if c.isdecimal() else c for c in (number or "")).strip()
		key = api.digits(number)
		if len(key) >= 3 and key not in seen:
			seen.add(key)
			out.append({"label": label, "number": number})
	return out


def _changed_rows(doctype, fields, since, until):
	"""Read each bounded page in (modified, name) order, including timestamp ties."""
	doc = frappe.qb.DocType(doctype)
	query = (
		frappe.qb.from_(doc)
		.select(doc.name, doc.modified, *(doc[field] for field in fields))
		.where((doc.modified > since) & (doc.modified <= until))
		.orderby(doc.modified, doc.name)
		.limit(BATCH)
	)
	page = query
	while rows := page.run(as_dict=True):
		yield from rows
		last = rows[-1]
		page = query.where((doc.modified > last.modified) | ((doc.modified == last.modified) & (doc.name > last.name)))


def push_contacts():
	"""Contacts and Leads with a phone number go to the PBX address book, so desk phones show
	the caller's name. Only what changed since the last run; the PBX updates by external_id."""
	s = api.settings()
	if not api.is_ready(s) or not s.push_contacts:
		return
	since = s.contacts_synced_at or "1900-01-01"
	started = frappe.utils.now_datetime()
	site = frappe.local.site
	sent = 0
	for row in _changed_rows("Contact", (), since, started):
		c = frappe.get_doc("Contact", row.name)
		numbers = _numbers(("mobile" if p.is_primary_mobile_no else "work", p.phone) for p in c.phone_nos)
		if numbers:
			sent += _send(f"erpnext:{site}:Contact:{c.name}", c.full_name or c.name, c.company_name or "", c.email_id or "", numbers)
	lead_fields = ("lead_name", "company_name", "email_id", "phone", "mobile_no")
	for lead in _changed_rows("Lead", lead_fields, since, started):
		numbers = _numbers([("work", lead.phone), ("mobile", lead.mobile_no)])
		if numbers:
			sent += _send(f"erpnext:{site}:Lead:{lead.name}", lead.lead_name or lead.name, lead.company_name or "", lead.email_id or "", numbers)
	frappe.db.set_single_value(api.SETTINGS, "contacts_synced_at", started)
	frappe.db.commit()
	return sent


def _send(external_id, name, company, email, numbers):
	email = (email or "").strip()
	payload = {
		"display_name": name[:150], "company": company[:120],
		"email": email if frappe.utils.validate_email_address(email) == email else "",
		"external_id": external_id, "numbers": numbers,
	}
	try:
		api.pbx_api("POST", "contacts", payload=payload)
	except api.PbxHttpError as error:
		if error.status != 422:
			raise
		frappe.log_error(title=f"AiPBX: invalid contact {external_id}"[:140], message=str(error))
		return 0
	return 1

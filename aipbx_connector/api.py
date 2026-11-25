"""AiPBX Connector for ERPNext / Frappe.

The PBX sends signed call events to `webhook`; each call becomes one ERPNext Call Log
(`aipbx-<linkedid>`), the user whose phone rings gets ERPNext's own caller pop-up, a missed call
leaves a "call back" ToDo, and the AI summary lands on the Call Log. Users place calls from any
phone field (`click_to_call`), and recordings are played through this site (`recording`), so the
PBX is never exposed to the browser.
"""

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
import uuid
from datetime import timezone
from urllib.parse import parse_qs, urlsplit

import frappe
import requests
from frappe import _

SETTINGS = "AiPBX Settings"
SIGNATURE_TOLERANCE = 300  # seconds either way
MAX_BODY = 1024 * 1024
ENDED = ("Completed", "No Answer", "Busy", "Failed", "Canceled")


# ---------------------------------------------------------------------------- the PBX

def settings():
	return frappe.get_cached_doc(SETTINGS)


def is_ready(s=None) -> bool:
	s = s or settings()
	return bool(s.enabled and s.pbx_url and s.connection_uuid)


class PbxHttpError(frappe.ValidationError):
	def __init__(self, status: int, message: str):
		super().__init__(message)
		self.status = status


def pbx_api(method: str, path: str, params=None, payload=None, stream=False):
	"""A call to the PBX REST API with the connection's token."""
	s = settings()
	if not is_ready(s):
		frappe.throw(_("AiPBX is not connected. Paste the connection pack in AiPBX Settings."))
	try:
		resp = requests.request(
			method,
			f"{s.pbx_url.rstrip('/')}/api/v1/{path}",
			params=params,
			json=payload,
			stream=stream,
			timeout=15,
			allow_redirects=False,
			headers={"Authorization": f"Bearer {s.get_password('api_token')}", "Accept": "application/json"},
		)
	except requests.RequestException as e:
		frappe.throw(_("The PBX did not answer: {0}").format(str(e)[:200]))
	if stream:
		if resp.status_code != 200:
			frappe.throw(_("The PBX answered {0}.").format(resp.status_code))
		return resp
	try:
		body = resp.json()
	except ValueError:
		body = {}
	if resp.status_code >= 400:
		error = body.get("error") if isinstance(body, dict) else body
		raise PbxHttpError(resp.status_code, _("The PBX answered {0}: {1}").format(resp.status_code, str(error or body)[:200]))
	return body


def digits(number) -> str:
	d = re.sub(r"\D", "", number or "")
	return d[2:] if d.startswith("00") else d


def check_signature(secret: str, timestamp, body: bytes, signature: str, now=None) -> bool:
	"""X-AiPBX-Signature-256 = "sha256=" + HMAC-SHA256(secret, timestamp + "." + raw body); the
	timestamp within five minutes. The raw bytes are signed, never re-serialised JSON."""
	if not secret or not timestamp or not signature:
		return False
	try:
		ts = int(timestamp)
	except (TypeError, ValueError):
		return False
	if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE:
		return False
	expected = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
	given = signature[7:] if signature.startswith("sha256=") else signature
	return hmac.compare_digest(expected, given)


# ---------------------------------------------------------------------------- webhook

def _answer(text: str, status: int):
	frappe.local.response.http_status_code = status
	return text


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(connection=None, **_kw):
	"""The PBX's signed deliveries. 2xx only once the event is stored; a repeat is 2xx too."""
	# A JSON body becomes the arguments and the query string is left out: read it from the URL.
	connection = frappe.request.args.get("connection") or connection
	body = frappe.request.get_data() or b""
	if len(body) > MAX_BODY:
		return _answer("too large", 413)
	s = settings()
	headers = frappe.request.headers
	if (
		not is_ready(s)
		or not connection
		or not hmac.compare_digest(str(connection), str(s.connection_uuid))
		or not check_signature(s.get_password("webhook_secret"), headers.get("X-AiPBX-Timestamp"), body,
			headers.get("X-AiPBX-Signature-256", ""))
	):
		# The same answer for an unknown connection and a bad signature.
		return _answer("unauthorized", 401)
	try:
		text = body.decode("utf-8")
		envelope = json.loads(text)
	except (UnicodeDecodeError, ValueError):
		return _answer("bad json", 400)
	if not isinstance(envelope, dict):
		return _answer("bad json", 400)
	event_id, event = envelope.get("event_id"), envelope.get("event")
	if not isinstance(event_id, str) or not event_id or not isinstance(event, str) or not event:
		return _answer("mismatch", 400)
	# The signed body and the headers must agree.
	if event_id != headers.get("X-AiPBX-Delivery") or event != headers.get("X-AiPBX-Event"):
		return _answer("mismatch", 400)

	old = frappe.db.get_value("AiPBX Event", {"event_id": event_id}, ["name", "payload"], as_dict=True)
	if old:
		# A repeat after a lost answer: fine. Another body under the same id: refused.
		return _answer("ok", 200) if old.payload == text else _answer("conflict", 409)
	rec = frappe.get_doc({"doctype": "AiPBX Event", "event_id": event_id, "event": event, "payload": text, "status": "Received"})
	frappe.db.savepoint("aipbx_webhook_insert")
	try:
		rec.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="aipbx_webhook_insert")
		old = frappe.db.get_value("AiPBX Event", {"event_id": event_id}, ["name", "payload"], as_dict=True, for_update=True)
		if old and old.payload != text:
			return _answer("conflict", 409)
		return _answer("ok", 200)
	frappe.db.commit()
	process_event(rec)
	frappe.db.set_single_value(SETTINGS, "last_event_at", frappe.utils.now_datetime())
	frappe.db.commit()
	return _answer("ok", 200)


def process_event(rec):
	"""Process and commit one event while holding its row lock."""
	current = frappe.db.get_value("AiPBX Event", rec.name, ["status", "attempts"], as_dict=True, for_update=True)
	if not current or current.status == "Done" or (current.attempts or 0) >= 5:
		frappe.db.commit()
		return
	frappe.db.savepoint("aipbx_event")
	try:
		envelope = json.loads(rec.payload)
		handle(envelope.get("event"), envelope.get("data") or {})
		rec.db_set({"status": "Done", "error": ""})
	except Exception:
		trace = frappe.get_traceback()
		frappe.db.rollback(save_point="aipbx_event")
		rec.db_set({"status": "Failed", "error": trace[-1000:], "attempts": (current.attempts or 0) + 1})
		frappe.log_error(title="AiPBX event failed")
	frappe.db.commit()


# ---------------------------------------------------------------------------- call events

def user_of(extension) -> str | None:
	"""The ERPNext user of a PBX extension (AiPBX Extension), or None."""
	ext = str(extension or "").strip()
	if not ext:
		return None
	user = frappe.db.get_value("AiPBX Extension", ext, "user")
	return user if user and frappe.db.get_value("User", user, "enabled") else None


def _offered_key(log_id: str) -> str:
	return f"aipbx:offered:{log_id}"


def _offered(log_id: str) -> list[str]:
	return frappe.cache.get_value(_offered_key(log_id)) or []


def _time(value):
	"""An ISO time from the PBX (with its offset) as this site's local time."""
	if not value:
		return None
	try:
		dt = frappe.utils.get_datetime(value)
	except Exception:
		return None
	if dt.tzinfo:
		dt = frappe.utils.convert_utc_to_system_timezone(dt.astimezone(timezone.utc).replace(tzinfo=None)).replace(tzinfo=None)
	return dt


def handle(event: str, data: dict):
	if event in ("call.ringing", "call.offered", "call.answered", "call.completed"):
		call_event(event, data)
	elif event == "recording.ready":
		recording_ready(data)
	elif event == "call.ai_completed":
		ai_completed(data)


def _call_log(data: dict, create=True):
	linkedid = str(data.get("linkedid") or "").strip()
	direction = data.get("direction") or "internal"
	if not linkedid:
		return None
	log_id = f"aipbx-{linkedid}"
	if frappe.db.exists("Call Log", log_id):
		return frappe.get_doc("Call Log", log_id, for_update=True)
	# Only live call events open a log (they carry the direction); calls between extensions get none.
	if not create or direction == "internal":
		return None
	caller = data.get("caller") or {}
	callee = data.get("callee") or {}
	doc = frappe.new_doc("Call Log")
	doc.id = log_id
	doc.medium = data.get("did") or ""
	doc.start_time = _time(data.get("started_at")) or frappe.utils.now_datetime()
	doc.status = "Ringing"
	if direction == "inbound":
		doc.type = "Incoming"
		doc.set("from", caller.get("number") or "")
		doc.to = data.get("agent_extension") or data.get("did") or ""
	else:
		doc.type = "Outgoing"
		doc.set("from", data.get("from_extension") or "")
		doc.to = callee.get("number") or ""
		if user := user_of(data.get("from_extension")):
			doc.employee_user_id = user
	# ERPNext links the Contacts and Leads with this number itself (before_insert).
	frappe.db.savepoint("aipbx_call_log_insert")
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="aipbx_call_log_insert")
		return frappe.get_doc("Call Log", log_id, for_update=True)
	return doc


def call_event(event: str, data: dict):
	doc = _call_log(data)
	if not doc:
		return
	if event == "call.offered":
		user = user_of(data.get("agent_extension"))
		offered = _offered(doc.id)
		if user and user not in offered:
			offered.append(user)
			frappe.cache.set_value(_offered_key(doc.id), offered, expires_in_sec=86400)
			if doc.type == "Incoming" and doc.status not in ENDED:
				# ERPNext's own caller pop-up, for the user whose phone rings.
				frappe.publish_realtime("show_call_popup", doc.as_dict(), user=user)
		if user and doc.type == "Incoming" and doc.status == "No Answer":
			# Completion can arrive before the agent offer. The missed call still needs follow-up.
			call_back_todo(doc, data, [user])
	elif event == "call.answered":
		if doc.status in ENDED:
			return
		doc.status = "In Progress"
		if data.get("agent_extension") and doc.type == "Incoming":
			doc.to = data["agent_extension"]
		if user := user_of(data.get("agent_extension")):
			doc.employee_user_id = user
		doc.save(ignore_permissions=True)
	elif event == "call.completed":
		if doc.status in ENDED:
			return
		outcome = data.get("outcome") or "missed"
		doc.status = "Completed" if outcome == "answered" else "No Answer"
		doc.duration = int(data.get("talk_seconds") or 0)
		doc.end_time = _time(data.get("ended_at")) or frappe.utils.now_datetime()
		if user := user_of(data.get("agent_extension")):
			doc.employee_user_id = user
		doc.save(ignore_permissions=True)
		which = "ended" if outcome == "answered" else "missed"
		for user in _offered(doc.id):
			frappe.publish_realtime(f"call_{doc.id}_{which}", doc.as_dict(), user=user)
		if outcome != "answered" and doc.type == "Incoming":
			call_back_todo(doc, data)


def call_back_todo(doc, data: dict, users: list[str] | None = None):
	"""A missed inbound call: a ToDo for the users whose phones rang, or the fallback user."""
	if users is None:
		users = _offered(doc.id) or [u for u in [settings().missed_call_user] if u]
	caller = data.get("caller") or {}
	number = caller.get("number") or doc.get("from") or ""
	name = (caller.get("contact") or {}).get("display_name") or caller.get("name") or ""
	if not name:
		# The Contact or Lead ERPNext linked by the number.
		for link in doc.get("links") or []:
			name = frappe.get_value(link.link_doctype, link.link_name, "full_name" if link.link_doctype == "Contact" else "lead_name") or ""
			if name:
				break
	for user in users:
		if frappe.db.exists("ToDo", {"reference_type": "Call Log", "reference_name": doc.name, "allocated_to": user}):
			continue
		frappe.get_doc({
			"doctype": "ToDo",
			"allocated_to": user,
			"description": _("Call back {0}").format(f"{name} ({number})" if name else number),
			"reference_type": "Call Log",
			"reference_name": doc.name,
			"priority": "Medium",
			"date": frappe.utils.today(),
		}).insert(ignore_permissions=True)


def recording_ready(data: dict):
	rec_id = data.get("recording_id")
	if not rec_id:
		return
	doc = _call_log(data, create=False)
	if not doc:
		raise ValueError("Recording arrived before its call log")
	doc.recording_url = f"/api/method/aipbx_connector.api.recording?call_log={doc.name}&id={int(rec_id)}"
	doc.save(ignore_permissions=True)


def ai_completed(data: dict):
	summary = (data.get("summary") or "").strip()
	if not summary:
		return
	doc = _call_log(data, create=False)
	if not doc:
		raise ValueError("AI summary arrived before its call log")
	doc.summary = summary[:2000]
	doc.save(ignore_permissions=True)
	note = _("AI summary of the call: {0}").format(frappe.utils.escape_html(summary))
	for link in doc.get("links") or []:
		frappe.get_doc(link.link_doctype, link.link_name).add_comment("Info", note)


# ---------------------------------------------------------------------------- for users

@frappe.whitelist()
def click_to_call(to_number: str, link_doctype: str | None = None, link_name: str | None = None):
	"""The user's own extension rings first, then the number is dialled. The extension comes from
	the user's mapping on the server, never from the browser."""
	ext = frappe.db.get_value("AiPBX Extension", {"user": frappe.session.user}, "name")
	if not ext:
		frappe.throw(_("You have no PBX extension. Ask an administrator to assign one in AiPBX Extension."))
	to = "".join(c for c in (to_number or "") if c.isdigit() or c in "+*#")
	if len(digits(to)) < 2:
		frappe.throw(_("There is no number to call."))
	pbx_api("POST", "calls/originate", payload={"from_extension": ext, "to_number": to, "client_request_id": str(uuid.uuid4())})
	return {"extension": ext, "number": to}


@frappe.whitelist()
def recording(call_log: str, id: str):
	"""Plays a call's recording through this site, for users who may read the Call Log."""
	doc = frappe.get_doc("Call Log", call_log)
	doc.check_permission("read")
	recording_ids = parse_qs(urlsplit(doc.recording_url or "").query).get("id", [])
	if not re.fullmatch(r"[1-9][0-9]*", id) or recording_ids != [id]:
		frappe.throw(_("Not this call's recording."), frappe.PermissionError)
	resp = pbx_api("GET", f"recordings/{id}", stream=True)
	frappe.local.response.filename = f"{call_log}.mp3"
	frappe.local.response.filecontent = resp.content
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"


def boot_session(bootinfo):
	"""Tells the desk whether click-to-call is there for this user."""
	if frappe.session.user == "Guest":
		return
	try:
		ready = is_ready()
	except Exception:
		ready = False
	bootinfo.aipbx = {"click_to_call": bool(ready and frappe.db.exists("AiPBX Extension", {"user": frappe.session.user}))}


# ---------------------------------------------------------------------------- settings

_BLOCKED_HOSTS = frozenset({
	"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
	"metadata.google.internal", "metadata",
})


def _is_blocked_ip(ip) -> bool:
	if getattr(ip, "ipv4_mapped", None) and _is_blocked_ip(ip.ipv4_mapped):
		return True
	return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def _validate_pbx_url(url: str) -> str:
	url = (url or "").strip().rstrip("/")
	if not url or any(c.isspace() or ord(c) < 32 for c in url):
		frappe.throw(_("The PBX address is not valid."))
	if "?" in url or "#" in url or ";" in url:
		frappe.throw(_("The PBX address must not contain a query string, parameters, or fragment."))
	try:
		parsed = urlsplit(url)
		port = parsed.port
	except ValueError:
		frappe.throw(_("The PBX address is not valid."))
	if parsed.scheme.lower() != "https":
		frappe.throw(_("The PBX address must start with https://"))
	if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
		frappe.throw(_("The PBX address must not contain credentials."))
	if parsed.query or parsed.fragment:
		frappe.throw(_("The PBX address must not contain a query string or fragment."))
	host = (parsed.hostname or "").strip().lower().rstrip(".")
	if not host:
		frappe.throw(_("The PBX address is missing a host name."))
	if port is not None and not (1 <= port <= 65535):
		frappe.throw(_("The PBX address port is not valid."))
	if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
		frappe.throw(_("The PBX address cannot target a loopback or metadata host."))
	try:
		ip = ipaddress.ip_address(host)
	except ValueError:
		try:
			socket.inet_aton(host)
			frappe.throw(_("The PBX address host is not a valid address."))
		except OSError:
			pass
		try:
			for info in socket.getaddrinfo(host, None):
				resolved = info[4][0].split("%", 1)[0]
				if _is_blocked_ip(ipaddress.ip_address(resolved)):
					frappe.throw(_("The PBX address cannot resolve to a private, loopback, or metadata IP."))
		except OSError:
			pass
	else:
		if _is_blocked_ip(ip):
			frappe.throw(_("The PBX address cannot target a private, loopback, link-local, or reserved IP."))
	return url


@frappe.whitelist()
def apply_pack(pack: str):
	"""The JSON the PBX shows once under Integration → Connections."""
	frappe.only_for("System Manager")
	try:
		p = json.loads(pack or "")
	except ValueError:
		frappe.throw(_("This is not a connection pack: paste the JSON the PBX showed."))
	need = ("pbx_url", "connection_id", "api_token", "webhook_secret")
	if not isinstance(p, dict) or any(not str(p.get(k) or "").strip() for k in need):
		frappe.throw(_("The connection pack is incomplete."))
	if int(p.get("aipbx_connection") or 0) > 1:
		frappe.throw(_("This connection pack is of a newer kind; update the AiPBX Connector app."))
	url = _validate_pbx_url(str(p["pbx_url"]))
	s = frappe.get_doc(SETTINGS)
	s.pbx_url = url
	s.connection_uuid = str(p["connection_id"]).strip()
	s.instance_id = str(p.get("instance_id") or "").strip()
	s.api_token = str(p["api_token"]).strip()
	s.webhook_secret = str(p["webhook_secret"]).strip()
	s.enabled = 1
	s.save()
	return {"pbx_url": url}


@frappe.whitelist()
def test_connection():
	frappe.only_for("System Manager")
	rows = pbx_api("GET", "extensions", params={"per_page": 500}).get("data") or []
	return _("The PBX answered: {0} extensions.").format(len(rows))


@frappe.whitelist()
def sync_extensions():
	"""Extensions from the PBX; a new one is mapped to the ERPNext user with the same e-mail."""
	frappe.only_for("System Manager")
	rows = pbx_api("GET", "extensions", params={"per_page": 500}).get("data") or []
	added = 0
	for row in rows:
		ext = str(row.get("extension") or "").strip()
		if not ext:
			continue
		label = row.get("full_name") or row.get("name") or ext
		if frappe.db.exists("AiPBX Extension", ext):
			frappe.db.set_value("AiPBX Extension", ext, "extension_name", label)
			continue
		email = (row.get("email") or "").strip().lower()
		user = email if email and frappe.db.get_value("User", email, "enabled") else None
		frappe.get_doc({"doctype": "AiPBX Extension", "extension": ext, "extension_name": label, "user": user}).insert(ignore_permissions=True)
		added += 1
	return _("{0} extensions from the PBX, {1} new.").format(len(rows), added)

# AiPBX Connector for ERPNext / Frappe

Connects ERPNext to the [AiPBX](https://github.com/mahirgul/AiPBX) phone system. Requires **AiPBX v1.81.0** or newer with the **ERPNext / Frappe** connection kind.

- **Caller pop-up:** ERPNext's own incoming-call pop-up, for the user whose phone rings.
- **Call Log:** every call with a customer becomes one ERPNext Call Log (incoming or outgoing, status,
  duration), linked by ERPNext to the Contact or Lead with that number.
- **Recordings:** played from the Call Log through ERPNext; the PBX is never exposed to the browser.
- **Missed calls:** a "Call back" ToDo for the users whose phones rang (or a fallback user).
- **AI summary:** written to the Call Log and as a comment on the linked Contact or Lead.
- **Click-to-call:** the phone icon of every phone field calls through the PBX: your own extension
  rings first, then the number is dialled.
- **Address book:** Contacts and Leads with a phone number go to the PBX every 5 minutes, so desk
  phones show the caller's name.

Tested end to end with ERPNext v16. Written for v15 too, not tested there yet.

## Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/mahirgul/AiPBX-Frappe
bench --site your.site install-app aipbx_connector
bench build --app aipbx_connector && bench restart
```

## Connect

1. In AiPBX: **Integration → Connections → Add**, kind **ERPNext / Frappe**, address of your ERPNext
   site (`https://erp.example.com`). Copy the connection pack it shows once.
2. In ERPNext: open **AiPBX Settings**, **Paste connection pack**, then **Test connection** and
   **Fetch extensions**.
3. In **AiPBX Extension**, check that every extension has its ERPNext user (an extension whose user
   has the same e-mail address is matched by itself). That user gets the pop-up and places calls
   from that extension.
4. Optional: **Missed calls go to** (a user for missed calls nobody's phone rang for), **Send Contacts
   and Leads to the PBX address book**.

Calls are signed by the PBX (HMAC-SHA256 with the pack's secret); a repeated delivery is stored once.
Turning the connection off in AiPBX stops the events and refuses the app's API key.

## How it works

| ERPNext | AiPBX |
|---|---|
| `aipbx_connector.api.webhook` (guest, signature-checked) | Connection webhook: `call.ringing/offered/answered/completed`, `recording.ready`, `call.ai_completed` |
| `aipbx_connector.api.click_to_call` | `POST /api/v1/calls/originate` |
| `aipbx_connector.api.recording` | `GET /api/v1/recordings/{id}` |
| `aipbx_connector.tasks.push_contacts` (every 5 minutes) | `POST /api/v1/contacts` (by `external_id`) |
| AiPBX Event (one row per delivery, retried by `tasks.retry_events`) | `event_id` of each delivery |

## License

MIT

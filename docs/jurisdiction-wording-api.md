# Jurisdiction wording API (DIAAT-342)

Document wording (hearing types, legal frameworks, anonymity statuses) is kept per
jurisdiction. An admin edits only the jurisdictions they are mapped to. A
jurisdiction with no legal frameworks has no legal wording: its judges' form has no
legal issues step and its documents have no Issues or Legal Framework sections.

All routes are under the API prefix the web app already uses. Auth is the same bearer
token as every other route. Admin roles come from Entra, as before: no Entra change.

## Concepts

- **Jurisdiction**: `{ id, name }`. `id` is a slug used in URLs (`iac`). `name` is the display name the
  judge's form submits as `form_data.jurisdiction`. `iac` is
  `First-tier Tribunal Immigration and Asylum Chamber` and holds all pre-existing wording.
- **has_legal_wording**: true when the jurisdiction has at least one legal framework.
- **Who may do what**
  - `LegalTextManager` (LTM): reads and edits wording of the jurisdictions mapped to them. Nothing else.
  - `SystemAdministrator`: reads every jurisdiction's wording; edits it only if they also hold
    `LegalTextManager`. Creates jurisdictions and manages who administers each.
  - Any signed-in user (judges): reads a jurisdiction's wording for the form (`GET /document-content`).
- An admin asking for a jurisdiction they may not use gets **404** `{"detail":"Jurisdiction not found"}`,
  identical to a jurisdiction that does not exist. Never 403, so nothing leaks. (403 still means
  "missing role", as before.)

## Admin: which jurisdictions can I edit?

`GET /admin/jurisdictions` (LTM or SystemAdministrator)

```json
[{ "id": "iac", "name": "First-tier Tribunal Immigration and Asylum Chamber",
   "has_legal_wording": true, "can_edit": true }]
```

- LTM: only their mapped jurisdictions (`can_edit` true). An empty list means they are an LTM with
  no jurisdiction yet: show "ask a system administrator to assign you a jurisdiction".
- SystemAdministrator: all jurisdictions; `can_edit` is true only if they also hold `LegalTextManager`
  (otherwise render read-only).
- Sorted by name. One entry: open it directly. Several: show a picker.

## Admin: read and edit one jurisdiction's wording

Replace the old unscoped paths with `/admin/jurisdictions/{jurisdiction_id}/document-content...`.

| Action | Route | Role |
| --- | --- | --- |
| Read all | `GET /admin/jurisdictions/{jid}/document-content` | LTM or SysAdmin |
| Hearing type | `PUT /.../hearing-types/{id}`, `POST /.../hearing-types` (201), `DELETE /.../hearing-types/{id}` (204) | LTM |
| Legal framework | `PUT /.../legal-frameworks/{id}`, `POST /.../legal-frameworks` (201), `DELETE /.../legal-frameworks/{id}` (204) | LTM |
| Anonymity status | `PUT /.../anonymity-statuses/{id}` | LTM |

(`...` is `/admin/jurisdictions/{jid}/document-content`.) Request bodies, `404` (item not found),
`409` (duplicate id) and item responses are unchanged from before.

`GET` response (the old fields, plus two):

```json
{ "jurisdiction": { "id": "tax", "name": "Tax Tribunal" },
  "has_legal_wording": false,
  "hearing_types": [ ... ], "legal_frameworks": [], "anonymity": { "statuses": [ ... ] } }
```

- When `has_legal_wording` is false, show the legal frameworks list empty with an "add" action: the first
  `POST .../legal-frameworks` gives the jurisdiction legal wording; deleting the last one removes it.
  The UI should say what this does (judges lose/gain the legal issues step).
- After a save, the next document a judge produces uses the new wording. No cache or delay to handle.

### Old unscoped routes (deprecated, kept so a web build deployed before this one keeps working)

`/admin/document-content...` (same methods and bodies) act on the `iac` jurisdiction, and are authorised
exactly like the scoped routes (an admin not mapped to `iac` gets 404). An optional `?jurisdiction_id=`
picks another jurisdiction; do not rely on it. They will be removed once the web app uses the scoped routes.

## Judge form: does this jurisdiction have legal wording?

`GET /document-content?jurisdiction=<id or display name>` (any signed-in user; `Cache-Control: no-store`)

- `jurisdiction` is optional and defaults to the IAC name, so existing clients are unchanged.
  Pass the value the form's jurisdiction field holds (the display name, or the id).
- Same response shape as the admin read above. **If `has_legal_wording` is false, hide the legal
  issues field/step** (`legal_frameworks` is `[]`); do not require it.
- Unknown jurisdiction: `404`. Refetch when the judge changes the jurisdiction field.

`GET /jurisdictions` (any signed-in user): options for the form's jurisdiction select.

```json
[{ "id": "iac", "name": "First-tier Tribunal Immigration and Asylum Chamber", "has_legal_wording": true }]
```

The form's `jurisdiction` option list is currently hard-coded to IAC; it can use this instead.

`POST /live-transcription/submission` is unchanged except: the wording used is that of
`form_data.jurisdiction` (default IAC); an unknown jurisdiction is refused with `422 "Unknown jurisdiction"`
before anything is stored; and for a jurisdiction with no legal wording the document has no Issues or Legal
Framework section (any `legalIssues` sent are ignored; no placeholder text is printed).

## SystemAdministrator: jurisdictions and who administers them

All SystemAdministrator only.

| Action | Route | Notes |
| --- | --- | --- |
| Create | `POST /admin/jurisdictions` body `{ "id": "tax", "name": "Tax Tribunal" }` | 201 `{id,name,has_legal_wording:false}`; `id` is `^[a-z0-9][a-z0-9_-]*$`, up to 64 chars; 409 if the id or name collides with any existing id or name, ignoring case (id against name too; checked atomically, so concurrent creates cannot both succeed), 422 if malformed. Starts with the shipped hearing types and anonymity wording and no legal frameworks. |
| List a jurisdiction's admins | `GET /admin/jurisdictions/{jid}/admins` | `[{ "user_id", "email", "role" }]` |
| Assign | `PUT /admin/jurisdictions/{jid}/admins/{user_id}` | 200 with the item; idempotent, also under concurrent identical requests (only the request that actually adds the mapping is audited). Pick the user with the existing `GET /admin/users?search_email=`. The user also needs the `LegalTextManager` role in Entra to reach the editor at all. |
| Unassign | `DELETE /admin/jurisdictions/{jid}/admins/{user_id}` | 204; 404 if they were not an admin of it |

A user appears in `/admin/users` only after they have signed in once, so a new admin signs in first, then
is assigned. Assignments and removals are logged and written to the audit log
(`JURISDICTION_ADMIN_ADDED` / `JURISDICTION_ADMIN_REMOVED`); denied attempts to use a jurisdiction are
audited as `ACCESS_DENIED` with `resource: "jurisdiction:<id>"`.

## Existing IAC admins

Migration `0004` maps every user whose role is `LegalTextManager` at that moment to `iac` and keeps the
existing IAC wording row untouched. They see one jurisdiction and their editor behaves as today. A
`LegalTextManager` who first signs in after the migration must be assigned by a SystemAdministrator.

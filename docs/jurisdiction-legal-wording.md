# Legal wording per jurisdiction (DIAAT-342)

"Legal wording" is what the IAC decision document is built from: the **hearing types**,
the **legal frameworks** (and the legal issues they carry) and the **anonymity** text.
Before DIAAT-342 it was one global row. Now each jurisdiction has its own, and its
LegalTextManager edits that one only, in the Document Content screen (Legal Frameworks
included). (Not to be confused with the *screen wording*, the 16 `WORDING_KEYS` in
`jurisdiction.settings.wording`: UI labels, not legal text, which the JurisdictionSuperAdministrator
edits from the Jurisdictions screen at `/api/admin/jurisdictions/{code}/wording`, and nobody else:
the in-jurisdiction route `/api/jurisdictions/wording` was removed in DIAAT-406.)

## Which jurisdiction

Every route below works in the request's **active jurisdiction**, chosen as for every
other route (`domain/jurisdictions.py`): the `X-Jurisdiction` header (a code, e.g. `ET`),
else the user's remembered choice, else their only jurisdiction. The web app already
sends the header from the tab's current jurisdiction. The jurisdiction being edited is
therefore always one the user belongs to, and the response says which it is.

## Who may edit (`/api/admin/document-content...`)

From DIAAT-373 the role is the one the user is **acting as** in the active jurisdiction (the
`ctx` of their BenchNotes token: `docs/authz-token-and-context.md`), not an Entra role.

| Active jurisdiction | Acting as | Read | Write |
|---|---|---|---|
| any | **LegalTextManager** in it (`legal_text.manage`) | yes | yes |
| any | everyone else: a Judge, a JurisdictionAdministrator, a JurisdictionSuperAdministrator, a LegalTextManager of another jurisdiction | no | no |

Since DIAAT-376 the permission is `legal_text.manage`, which only the LegalTextManager holds. A
JurisdictionAdministrator (who could edit it under DIAAT-342) edits neither the legal text nor, since
DIAAT-406, the screen wording (`jurisdiction.wording.edit` is the JurisdictionSuperAdministrator's); it
gives the LegalTextManager role in its jurisdiction. A SystemAdministrator no longer reads
the IAC's legal wording either (DIAAT-389: system-level work only). The LegalTextManager is held in a
jurisdiction (it was one global Entra role that meant the IAC).

Errors (same shapes as the screen wording editor):

- `409 {"detail": {"code": "context_changed", ...}}`: the header names a jurisdiction that is
  not the one the user's token acts in (a stale tab). `403 jurisdiction_not_available`: the
  token's jurisdiction has been disabled since. Read and write alike.
- `403 {"detail": {"code": "permission_denied", "message": ...}}`: someone acting as a role that
  may not edit legal wording (was `jurisdiction_admin_required`). The refusal is audited
  (`ACCESS_DENIED`).
- `403 context_required` (no context chosen) / `403 no_jurisdiction` (a global role): as everywhere else.
- `404 {"detail": "Not available in this jurisdiction"}`: the jurisdiction does not use
  the `iac_decision` template (it has no legal wording, as for its judges' form).

Responses are `Cache-Control: no-store`.

## How the UI learns what to show

- `GET /api/me/permissions` says what the user is acting as (`active.role`), and
  `GET /api/jurisdictions/current` that the jurisdiction's `template == "iac_decision"`, which means
  it has legal wording at all. Show the legal wording editor for a LegalTextManager (`legal_text.manage`) when it does.
- The editor's own `GET /api/admin/document-content` says which jurisdiction it edits
  (`jurisdiction`) and whether it has legal wording (`has_legal_wording`).

## Read

`GET /api/admin/document-content` -> 200

```json
{
  "jurisdiction": {"code": "ET", "name": "Employment Tribunal"},
  "has_legal_wording": false,
  "hearing_types": [{"id": "face_to_face", "title": "...", "hearing_title": "...", "hearing_description": "markdown"}],
  "legal_frameworks": [],
  "anonymity": {"statuses": [{"id": "granted", "title": "...", "content": "...", "identifier": "...",
                              "header": "...", "order_header": "...", "order_text": "..."}]}
}
```

A jurisdiction that has never saved starts with the shipped hearing types and anonymity
text (a document needs them) and **no legal frameworks**, so `has_legal_wording` is
`false`. `has_legal_wording` is simply "there is at least one legal framework".

## Write

The existing routes and bodies, unchanged, now acting on the active jurisdiction's own
wording (a jurisdiction's wording is created on its first save):

| Route | Body | Success |
|---|---|---|
| `PUT /api/admin/document-content/hearing-types/{id}` | hearing type | 200 item; 404 unknown id |
| `POST /api/admin/document-content/hearing-types` | hearing type | 201 item; 409 id exists |
| `DELETE /api/admin/document-content/hearing-types/{id}` | | 204; 404 |
| `PUT /api/admin/document-content/legal-frameworks/{id}` | legal framework | 200 item; 404 |
| `POST /api/admin/document-content/legal-frameworks` | legal framework | 201 item; 409 id exists |
| `DELETE /api/admin/document-content/legal-frameworks/{id}` | | 204; 404 |
| `PUT /api/admin/document-content/anonymity-statuses/{id}` | anonymity status | 200 item; 404 |

Adding a jurisdiction's **first** legal framework turns its legal wording on; deleting
its **last** turns it off (for the IAC too). There is no version/409 check: as before,
the last save wins.

Every change is audited as `jurisdiction.edit` with
`detail = {"code", "fields": ["legal_wording"], "change", "item"}` (the item's id, never
the wording).

## Judges: the form

`GET /api/document-content` (judge route, unchanged path) returns the **active
jurisdiction's** wording, with one new field:

```json
{"hearing_types": [...], "legal_frameworks": [...], "anonymity": {...}, "has_legal_wording": true}
```

When `has_legal_wording` is `false` the form must have **no legal issues step or field**
(the hearing types and anonymity are still there). Still 404 for a jurisdiction without
the `iac_decision` template. The judge reads the new wording on the very next request
(the server's cache is keyed on the row's `updated_at`).

## The document

`POST /api/live-transcription/submission` renders with the **active jurisdiction's** legal
wording (the `form_data.jurisdiction` free-text field is ignored). When it has no legal
wording the document **omits the Issues and Legal Framework sections entirely**: no
headings, no "The parties agreed ..." intro, no placeholders, whatever `legalIssues` the
form still carries. IAC documents are byte-identical to before (pinned by
`tests/unit/dictation/unit/app/documents/test_iac_document_unchanged.py`).

Only the structured renderer (the one the API uses) was changed. The older
template-based renderer (`render_document(DocumentData)`, not used by the API) still
fills the bundled template's Issues and Legal Framework controls and was left alone.

## Data

Migration `0006`: `document_content.jurisdiction_id` (NOT NULL, FK, unique). The
existing row becomes the IAC's, **id 1 stays the IAC's** (the id sequence restarts at 2
and a check constraint refuses id 1 for any other jurisdiction), and its `content` and
`updated_at` are not touched. A jurisdiction has no row until its first save. Only the
IAC falls back to the bundled `data/document_content.json` when the database is down (and
seeds its row from it on first read); any other jurisdiction then gets an error, never
the IAC's wording. The downgrade keeps only the IAC's row (other jurisdictions' legal
wording is deleted).

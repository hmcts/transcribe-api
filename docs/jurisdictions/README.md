# Jurisdictions (DIAAT-322)

BenchNotes serves any number of jurisdictions from one service. Each one is a row in
the `jurisdiction` table. Its screen wording and the parts of BenchNotes it uses are
stored in `settings`, so adding a jurisdiction does not need a deployment. The IAC is
created by migration `0004`, with the wording the web app showed before DIAAT-322.

`0004` is additive, and `0005` completes it by allowing a draft per jurisdiction.
Both run in the same release: each pod migrates to head as it starts. While the
rollout lasts, the previous release's pods keep serving. Their draft saves fail
for those few minutes (the page keeps the text and saves again), and everything
else they write lands in the IAC. Someone signing in for the first time through
one of those pods gets no jurisdiction; add them on the Jurisdictions tab.

A SystemAdministrator manages them in the admin area, on the **Jurisdictions** tab
(DIAAT-356): add or edit a jurisdiction (its name, wording, template, hearing
recordings, enabled), and add or remove its members by email, as judge
or admin (these are now role assignments: DIAAT-382). Behind it is `/api/admin/jurisdictions`. Every change is audited. A save
made from a form opened before someone else's change is refused (409), not applied
over it.

## Setting up a new jurisdiction (DIAAT-345)

A new jurisdiction goes through three **stages**, without a release:

| Stage | Who of its members sees it |
|---|---|
| **Draft** (every new jurisdiction) | its admins only, to set it up |
| **Test** | its admins, and the members marked **tester** (the test group) |
| **Live** | every member |

**Enabled** still switches a jurisdiction off for everyone, whatever its stage. Every
jurisdiction from before stages is live (migration `0007`), so nothing changes for it.

**Deploying the release that adds stages:** wait for its rollout to finish before you
create a jurisdiction or change one's stage. Until then, pods still running the
previous release keep serving. They do not know about stages, so they would show an
enabled draft or test jurisdiction to every member (or keep showing a jurisdiction
moved from live to test to members who are not testers). This applies only to the first
deployment of this release in each environment; after it, every pod enforces stages.

A jurisdiction leaves draft only when it is ready. The admin area lists what is
missing, and moving it to test or live is refused (400 `jurisdiction_not_ready`) until:

- its required screen wording is set (`REQUIRED_WORDING_KEYS` in
  `domain/jurisdictions.py`, plus the navigation wording when it uses hearing
  recordings): the other keys have a neutral default that suits any jurisdiction;
- it has at least one admin.

A jurisdiction also has an optional **display name**, which is what users see (for
example "Employment Tribunals (ET)"), and an optional **service**, where it applies.

A user acting as the **JurisdictionAdministrator** of a jurisdiction changes that
jurisdiction's wording, and nothing else, from the **Wording** link in the header
(`/jurisdiction-wording`, behind it `/api/jurisdictions/wording`). Being an administrator
in one jurisdiction gives nothing in another.

A jurisdiction's **legal wording** (hearing types, legal frameworks, anonymity text: what
its decision document is built from) is its own too, edited by its admins and, for the
IAC, by the LegalTextManager role as before: see
[`../jurisdiction-legal-wording.md`](../jurisdiction-legal-wording.md) (DIAAT-342).

A user's roles are `role_assignment` rows, and they act in one (jurisdiction, role) at a
time, chosen after sign-in: see [`../authz-token-and-context.md`](../authz-token-and-context.md).
Nobody is enrolled automatically (DIAAT-373): a new user has no role until an administrator adds
them (or, later, approves their request).

The same operations run from a command line, for local development:

```sh
python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
python -m transcribe_api.jurisdictions_admin add-member ET lead.admin@ejudiciary.net --role admin
python -m transcribe_api.jurisdictions_admin add-member ET first.last@ejudiciary.net --role judge --tester
python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" --stage test < et.json
python -m transcribe_api.jurisdictions_admin list
```

`put` changes `--enabled` (and `--no-enabled`), `--stage`, `--display-name` and
`--service` only when you give them. A new jurisdiction starts enabled, as a draft.
There is no auto-enrol any more.

- **Add judges before they first sign in.** A new user claims any assignments added
  for their email (whatever its case). A new user with none has no role and sees "no access
  yet". An ET test judge added in advance therefore sees only the ET.
- **Settings** are validated before they are saved (`JurisdictionSettings` in
  `domain/jurisdictions.py`):
  - `wording`: only the keys in `WORDING_KEYS`. Any key a jurisdiction does not set
    uses the web app's neutral default.
  - `template`: `iac_decision` (the IAC hearing form and decision document) or `null`.
    A jurisdiction with no template dictates into a general section and copies its
    text out, unless it has uploaded templates (below).
  - `features.hearing_recordings`: whether the jurisdiction uses the hearing
    recordings area.
- **[`et.example.json`](et.example.json)** is a starting point for the ET. Its wording
  is a placeholder until the ET's own wording is agreed.

A request works in the jurisdiction of the user's active context (their BenchNotes token's
`ctx`). The `X-Jurisdiction` header the web app sends for its tab is advisory: if it differs
from the token's context the answer is 409 `context_changed`.

## Uploaded Word templates (DIAAT-344)

A jurisdiction can have any number of Word templates, uploaded in the admin area on the
**Jurisdictions** tab (SystemAdministrator only), with no code change or release.
Judges of the jurisdiction choose one on the recording page and fill its Hearing
Details form; their document is the template with its boxes filled.

- **The boxes** are the template's content controls (Word: Developer > Controls). Each
  needs a tag (Properties > Tag), else its title is used. Boxes with the same tag are one
  field and all get its value. A Word dropdown's items become the field's options
  ("Choose an item." prompts are left out); a date picker becomes a date field.
- **The field list.** An upload proposes a field for each box: its title as the label,
  its type, required. The admin changes the label, type, required/optional, options and
  order, but cannot add a field: every field is one of the template's boxes. Types:
  text, date, dropdown, multi-select, yes/no, number, legal issues (the jurisdiction's
  legal frameworks, kept by its Legal Text Manager), filled by BenchNotes (the
  jurisdiction's name, or the year) and dictated text (the judgment: what was dictated,
  then the judge's own text). Exactly one box takes the dictated text.
- **Saving** is refused (400 `template_not_ready`) until every box has a complete field;
  `problems` names each box or field. Only then can it be enabled for judges.
- **A new file** for a template (`?replace=<id>&version=<version>`) keeps the fields of
  the boxes it still has. A new box has no field yet, so the template is switched off
  for judges until the admin links it.
- **Empty optional boxes** are removed, with their paragraph when every box in it is
  empty (an unused party or panel member leaves no line). Text outside the boxes stays,
  so an "and" between parties in a paragraph of its own is left behind until template
  owners group each party block in one content control.

The upload is the raw request body, `POST /api/admin/jurisdictions/{code}/templates`
with `Content-Type: application/octet-stream` and the file's name in
`X-Upload-Filename` (URL-encoded), as the browser's other uploads: Front Door's WAF
blocks multipart bodies and lets this path through by a custom Allow rule
(`BenchNotesUploadAllow`, azure-platform-terraform). Until that rule is deployed in an
environment, uploads there are blocked at Front Door.

The file is checked before it is read: at most 2 MB (30 MB unpacked), a Word document
(.docx, not macro-enabled or .dotx), and no macros, ActiveX, embedded objects or external
links Word would fetch (an image pasted from an email, `cid:`, is fine), whether a
relationship or a field that fetches content when Word updates fields (INCLUDEPICTURE,
INCLUDETEXT, LINK, DDE, DDEAUTO, IMPORT; simple or complex, in any part). It is stored in
the database (`jurisdiction_template`, migration `0008`) with its SHA-256, which is the
document's template version in its provenance.

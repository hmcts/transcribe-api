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

A JurisdictionSuperAdministrator (any jurisdiction, and creating one) or a JurisdictionAdministrator (the
one it works in) manages them in the admin area, on the **Jurisdictions** tab (DIAAT-356, DIAAT-376;
a SystemAdministrator has no access to them): add or edit a jurisdiction (its name, wording, template, hearing
recordings, enabled). Who holds a role in a jurisdiction is a role assignment (DIAAT-382), given and taken away on
the **Users** tab through `/api/admin/assignments` (DIAAT-383: [`../authz-admin-api.md`](../authz-admin-api.md)).
Behind the Jurisdictions tab is `/api/admin/jurisdictions`. Every change is audited. A save
made from a form opened before someone else's change is refused (409), not applied
over it.

## Setting up a new jurisdiction (DIAAT-345)

A new jurisdiction goes through three **stages**, without a release:

| Stage | Who of its members sees it |
|---|---|
| **Draft** (every new jurisdiction) | its admins only, to set it up |
| **Test** | its admins, and the members marked **tester** (the test group) |
| **Live** | every member |

**Enabled** still switches a jurisdiction off for everyone, whatever its stage (and nobody
can be given a role in a disabled one). Every jurisdiction from before stages is live
(migration `0007`), so nothing changes for it.

A user acts in a context they hold (a role in a jurisdiction: see
[`../authz-token-and-context.md`](../authz-token-and-context.md)), and a context in a jurisdiction
their stage does not let them see is not offered to them (`domain/assignments.load_assignments`,
`domain/jurisdiction_stages.sees`). A JurisdictionAdministrator is offered it at every stage (with
Judge and LegalTextManager there), a **tester** (the `tester` flag of a Judge or LegalTextManager
assignment, set when it is made: `POST /api/admin/assignments` with `"tester": true`) from the test
stage, and everyone else only when it is live. The stage, display name and
service of a jurisdiction are changed by a JurisdictionSuperAdministrator only, as its name and
whether it is on.

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

A jurisdiction's screen wording is changed only by a **JurisdictionSuperAdministrator**, in
the Jurisdictions screen (`/api/admin/jurisdictions/{code}/wording`, DIAAT-406). A
JurisdictionAdministrator does not edit it.

A jurisdiction's **legal wording** (hearing types, legal frameworks, anonymity text: what
its decision document is built from) is its own too, edited by its LegalTextManager: see
[`../jurisdiction-legal-wording.md`](../jurisdiction-legal-wording.md) (DIAAT-342).

A user's roles are `role_assignment` rows, and they act in one (jurisdiction, role) at a
time, chosen after sign-in: see [`../authz-token-and-context.md`](../authz-token-and-context.md).
Nobody is enrolled automatically (DIAAT-373): a new user has no role until an administrator adds
them (or, later, approves their request).

Creating and updating jurisdictions runs from a command line too, for local development (the members commands are gone: use the
assignment endpoints):

```sh
python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
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
  - `template`: no longer used. There is no built-in template for one jurisdiction:
    every jurisdiction's judges choose from its uploaded templates (below), and one
    with none dictates into a general section and copies its text out. A stored
    `iac_decision` (the IAC's, from migration `0004`) and one sent by an older release's
    admin page are accepted and dropped; `GET /api/jurisdictions/current` gives `null`.
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
Details form; their document is the template with its boxes filled. These are the only
templates: the built-in IAC decision is gone, so the IAC uploads its decision template
too (until it does, its judges dictate and copy their text out).

- **The boxes** are the template's content controls (Word: Developer > Controls). Each
  needs a tag (Properties > Tag), else its title is used. Boxes with the same tag are one
  field and all get its value. A Word dropdown's items become the field's options
  ("Choose an item." prompts are left out); a date picker becomes a date field.
- **The field list.** An upload proposes a field for each box: its title as the label,
  its type, required. The admin changes the label, type, required/optional, options and
  order, but cannot add a field: every field is one of the template's boxes. Types:
  text, date, dropdown, multi-select, yes/no, number; legal issues, hearing type and
  anonymity, answered from the jurisdiction's Document Content (kept by its Legal Text
  Manager, at most one box each); filled by BenchNotes; and dictated text.
- **Document Content** fills boxes as the built-in IAC decision did. The legal issues box
  lists the chosen frameworks' questions, `(a)`, `(b)`..., and the hearing type box its
  section title. "Filled by BenchNotes" boxes take the jurisdiction's name or the year,
  or follow an answer: the legal framework text of the issues chosen, the hearing
  type's description, and the anonymity reasons, order heading and order text (left
  out, with the box's paragraph, where the Document Content says `[DELETE]`); such a box
  needs the box it follows. Markdown keeps its headings, bold, lists and quotes. Tags
  the IAC's decision template uses (`LegalIssues`, `LegalFrameworkContent`,
  `HearingType`, `HearingDescription`, `AnonymityHead`, `AnonymityReasonsText`,
  `AnonymityOrderText`, `Background`, `Evidence`, `Facts`...) are proposed as these.
- **Dictated sections.** Every dictated box is a section (Background, Evidence...) the
  judge picks while dictating, by its name, so no two may share one; at least one is
  needed. Each gets what was dictated into it, then "Your notes" and the part of the
  notes under a heading with its name (text before any is the first section's), as the
  built-in decision did, or `[No <name> content]`. `GET /api/jurisdictions/templates`
  lists each template's `sections`; a submission's `messages` are by section tag (a
  list, or a section the template does not have, goes to the first). A box that is all
  its paragraph holds becomes a box around paragraphs, in that paragraph's style.
- **Saving** is refused (400 `template_not_ready`) until every box has a complete field;
  `problems` names each box or field. Only then can it be enabled for judges.
- **A new file** for a template (`?replace=<id>&version=<version>`) keeps the fields of
  the boxes it still has. A new box has no field yet, so the template is switched off
  for judges until the admin links it.
- **Asked only when** (`show_when: {tag, answers}`): a question is asked only when
  another question with set answers (a list, yes/no as `yes`/`no`, or a Document Content
  one by id) was answered with one of `answers`, as the built-in form asked "Specify
  location" only after "Other". A question not asked is not required, a value sent for
  it is dropped, and so is one asked after it.
- **Starts with** (`prefill`, text questions): `judge_name` or `judge_title`, which the
  web app fills from the signed-in user's email as the built-in form did; the judge can
  change it.
- **When left empty** (`empty_text`): what a question's box prints when it has no answer
  (or was not asked), such as a standard fee award line.
- **Empty optional boxes** with no `empty_text` are removed, with their paragraph when every box in it is
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
document's template version in its provenance; when boxes were filled from Document
Content, the version combines the file's hash with that content's.

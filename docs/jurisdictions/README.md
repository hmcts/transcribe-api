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
recordings, auto-enrol, enabled), and add or remove its members by email, as judge
or admin. Behind it is `/api/admin/jurisdictions`. Every change is audited. A save
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

A member who is an **admin** of a jurisdiction changes that jurisdiction's wording,
and nothing else, from the **Wording** link in the header (`/jurisdiction-wording`,
behind it `/api/jurisdictions/wording`). Being an admin in one jurisdiction gives
nothing in another.

A jurisdiction's **legal wording** (hearing types, legal frameworks, anonymity text: what
its decision document is built from) is its own too, edited by its admins and, for the
IAC, by the LegalTextManager role as before: see
[`../jurisdiction-legal-wording.md`](../jurisdiction-legal-wording.md) (DIAAT-342).

A membership does not replace the BenchNotes app role in Entra: someone with neither
role cannot sign in, whatever their memberships.

The same operations run from a command line, for local development:

```sh
python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" < et.json
python -m transcribe_api.jurisdictions_admin add-member ET first.last@ejudiciary.net --role judge --tester
python -m transcribe_api.jurisdictions_admin put ET --name "Employment Tribunal" --stage test < et.json
python -m transcribe_api.jurisdictions_admin list
```

`put` changes `--auto-enrol`, `--enabled` (and their `--no-` forms), `--stage`,
`--display-name` and `--service` only when you give them. A new jurisdiction starts
enabled, as a draft, without auto-enrol.

- **Add judges before they first sign in.** A new user claims any memberships added
  for their email. A new user with none is made a judge in every `auto_enrol`
  jurisdiction (the IAC), which is how sign-up worked before jurisdictions. An ET test
  judge added in advance therefore sees only the ET.
- **Settings** are validated before they are saved (`JurisdictionSettings` in
  `domain/jurisdictions.py`):
  - `wording`: only the keys in `WORDING_KEYS`. Any key a jurisdiction does not set
    uses the web app's neutral default.
  - `template`: `iac_decision` (the IAC hearing form and decision document) or `null`.
    A jurisdiction with no template dictates into a general section and copies its
    text out.
  - `features.hearing_recordings`: whether the jurisdiction uses the hearing
    recordings area.
- **[`et.example.json`](et.example.json)** is a starting point for the ET. Its wording
  is a placeholder until the ET's own wording is agreed.

A request works in the jurisdiction named by its `X-Jurisdiction` header (the web app
sends the one its tab is in). Without the header it uses the user's last choice, or
their only jurisdiction.

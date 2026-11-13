# Permissions: who may call what (DIAAT-373 Phase 3, DIAAT-376, DIAAT-403, DIAAT-406)

Every route of the API declares exactly one thing about who may call it, and a test generated from the
application proves it: `tests/unit/test_permission_matrix.py`. Nothing decides access by a role name any more:
the role is turned into permissions in one place (`domain/auth/permissions.py`) and routes ask for permissions
(`domain/auth/authorisation.py`).

## How a request is decided

1. The BenchNotes token is verified locally (`docs/authz-token-and-context.md`); no database read.
2. `require_permission(*permissions)` takes the permissions of the context the caller acts in (the token's `ctx.role`), and nothing
   from the other roles the caller holds (DIAAT-403). A SystemAdministrator (token claim `sysadmin`) has the system-level ones only while
   acting in the SystemAdministrator context (`{"jurisdiction": null, "role": "SystemAdministrator"}`). The route needs **one of**
   the permissions it lists.
3. Refusals are 403 `{"detail": {"code", "message"}}`:

| code | when | audited |
|---|---|---|
| `context_required` | no context is chosen (nobody holds a permission without one) | no (as before: a context is not a role) |
| `permission_denied` | the active role does not grant the permission | yes: `ACCESS_DENIED`, outcome `denied`, with the active jurisdiction and role, the role(s) held and the permissions asked for |
| `dictation_disabled` | the caller holds `dictation.use` but dictation is switched off (DIAAT-388) | yes, as above |
| `jurisdiction_not_in_scope` | a jurisdiction-scoped permission used on another jurisdiction than the one the caller acts in (a JurisdictionSuperAdministrator's apply everywhere) | yes |

4. A route has no permission only if it carries an explicit exemption marker (below). A route with neither, or both, fails the test suite.

`permission_denied` replaces the per-route codes that existed before (`jurisdiction_admin_required` on the wording and
legal-wording routes).

## The role map

| Permission | Judge | LegalTextManager | JurisdictionAdministrator | JurisdictionSuperAdministrator | SystemAdministrator (context, needs the token's `sysadmin`) |
|---|---|---|---|---|---|
| app.access, context.switch, access.request | yes | yes | yes | yes | |
| dictation.use, recording.upload, recording.own, documents.export, recording.audit.read | yes | | | | |
| document_content.read | yes | yes | | | |
| document_content.write, legal_text.manage | | yes | | | |
| admin.access | | yes | yes | yes | yes |
| users.manage, access.approve, templates.manage | | | yes | | |
| role_audit.read | | | yes (its jurisdiction) | yes (all) | |
| accounts.manage | | | | yes | |
| jurisdiction.create, jurisdictions.manage, jurisdiction.wording.edit, jurisdiction.features, jurisdiction_admin.assign | | | | yes | |
| jurisdiction_super_admin.assign | | | | | yes |
| feature_flags.manage, system.settings, security.alert_test | | | | | yes |
| recording.any.read, recording.any.edit, recording.any.delete, dictation.any.read | nobody | | | | |

Every role a user acts in is one they were given (DIAAT-406): none is derived from another. A JurisdictionAdministrator is not offered
Judge or LegalTextManager unless it holds them too ([`authz-token-and-context.md`](authz-token-and-context.md)).

### Who manages what (DIAAT-406)

| Role | Screens |
|---|---|
| Judge | Dictation, Recordings |
| LegalTextManager | Document Content (hearing types, legal frameworks, anonymity) |
| JurisdictionAdministrator | Users (with its jurisdiction's role history), Templates, Approvals (its jurisdiction) |
| JurisdictionSuperAdministrator | Jurisdictions (settings, screen wording, appointing JurisdictionAdministrators); User accounts (deactivate and reactivate any account, any user's role history across all jurisdictions) |
| SystemAdministrator | Super administrators, Feature flags |

Who gives which role: a SystemAdministrator JurisdictionSuperAdministrator; a JurisdictionSuperAdministrator JurisdictionAdministrator (any
jurisdiction, from the Jurisdictions screen); a JurisdictionAdministrator Judge and LegalTextManager (its jurisdiction). Each may give the role to
itself too. Judge and LegalTextManager are asked for by access request, which only a JurisdictionAdministrator decides. Rules:
[`authz-admin-api.md`](authz-admin-api.md).

Jurisdiction-scoped permissions (`permissions.JURISDICTION_SCOPED_PERMISSIONS`: users.manage, jurisdictions.manage,
jurisdiction.wording.edit, jurisdiction.features, templates.manage, access.approve, role_audit.read) apply in the jurisdiction the
caller acts in, or in every jurisdiction for the JurisdictionSuperAdministrator (whose scoped permissions are the jurisdiction ones:
`jurisdictions.manage`, `jurisdiction.wording.edit`, `jurisdiction.features`). A route that names a jurisdiction
(`/admin/jurisdictions/{code}...`) calls `assert_jurisdiction_in_scope(user, code)`; a route that works on the active
jurisdiction is in scope by construction. A route that needs a second permission on some paths calls
`ensure_permission(user, permission)` (creating a jurisdiction needs `jurisdiction.create` on top of `jurisdictions.manage`).

## Every route

Roles: **LTM** LegalTextManager, **JA** JurisdictionAdministrator, **JSA** JurisdictionSuperAdministrator, **sysadmin** a
SystemAdministrator acting as SystemAdministrator (in no other context). "Who" is derived from the map: the roles whose permissions
include one the route asks for, as pinned per role in `tests/unit/permission_matrix_roles_snapshot.json`. (`/api/v1` is the hearing-recording area, the rest of `/api` is the dictation product and
the shared routes.)

| Method | Path | Permission (any of) | Who |
|---|---|---|---|
| `GET` | `/api/health` | (exempt: public) | - |
| `GET` | `/api/health/dependencies` | (exempt: public) | - |
| `POST` | `/api/security/alert-test` | `security.alert_test` | sysadmin |
| `GET` | `/api/healthcheck` | (exempt: public) | - |
| `GET` | `/api/document-content` | `document_content.read` | Judge, LTM |
| `GET` | `/api/user/onboarding-status` | `app.access` | Judge, LTM, JA, JSA |
| `POST` | `/api/user/complete-onboarding` | `app.access` | Judge, LTM, JA, JSA |
| `POST` | `/api/user/reset-onboarding` | `app.access` | Judge, LTM, JA, JSA |
| `GET` | `/api/healthcheck/azure-storage` | `system.settings` | sysadmin |
| `POST` | `/api/get-upload-url` | `dictation.use` | Judge |
| `PUT` | `/api/upload-audio` | `dictation.use` | Judge |
| `POST` | `/api/generate-or-edit-minutes` | `dictation.use` | Judge |
| `GET` | `/api/templates` | `document_content.read` | Judge, LTM |
| `GET` | `/api/transcriptions-metadata` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}` | `dictation.use` | Judge |
| `POST` | `/api/transcriptions` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/document-download` | `documents.export` | Judge |
| `GET` | `/api/download` | `documents.export` | Judge |
| `DELETE` | `/api/transcriptions/{transcription_id}` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/minute-versions/{minute_version_id}` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/minute-versions` | `dictation.use` | Judge |
| `POST` | `/api/transcriptions/{transcription_id}/minute-versions` | `dictation.use` | Judge |
| `POST` | `/api/transcriptions/{transcription_id}/jobs` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/jobs` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/jobs/{job_id}/audio` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/sectioned-transcript` | `dictation.use` | Judge |
| `POST` | `/api/live-transcription/submission` | `dictation.use` | Judge |
| `GET` | `/api/transcriptions/{transcription_id}/tags` | `dictation.use` | Judge |
| `POST` | `/api/transcriptions/{transcription_id}/tags` | `dictation.use` | Judge |
| `DELETE` | `/api/transcriptions/{transcription_id}/tags/{tag_name}` | `dictation.use` | Judge |
| `GET` | `/api/user` | `app.access` | Judge, LTM, JA, JSA |
| `GET` | `/api/users/me` | `app.access` | Judge, LTM, JA, JSA |
| `GET` | `/api/user/profile` | `app.access` | Judge, LTM, JA, JSA |
| `POST` | `/api/user` | `app.access` | Judge, LTM, JA, JSA |
| `POST` | `/api/langfuse/trace` | `dictation.use` | Judge |
| `POST` | `/api/langfuse/score` | `dictation.use` | Judge |
| `GET` | `/api/get-speech-token` | `dictation.use` | Judge |
| `POST` | `/api/generate-realtime-summary` | `dictation.use` | Judge |
| `GET` | `/api/live-draft` | `dictation.use` | Judge |
| `PUT` | `/api/live-draft` | `dictation.use` | Judge |
| `DELETE` | `/api/live-draft` | `dictation.use` | Judge |
| `GET` | `/api/admin/me` | `admin.access` | LTM, JA, JSA, sysadmin |
| `GET` | `/api/admin/roles` | `users.manage` or `jurisdiction_admin.assign` or `jurisdiction_super_admin.assign` | JA, JSA, sysadmin |
| `GET` | `/api/admin/document-content` | `legal_text.manage` | LTM |
| `PUT` | `/api/admin/document-content/hearing-types/{item_id}` | `legal_text.manage` | LTM |
| `POST` | `/api/admin/document-content/hearing-types` | `legal_text.manage` | LTM |
| `DELETE` | `/api/admin/document-content/hearing-types/{item_id}` | `legal_text.manage` | LTM |
| `PUT` | `/api/admin/document-content/legal-frameworks/{item_id}` | `legal_text.manage` | LTM |
| `POST` | `/api/admin/document-content/legal-frameworks` | `legal_text.manage` | LTM |
| `DELETE` | `/api/admin/document-content/legal-frameworks/{item_id}` | `legal_text.manage` | LTM |
| `PUT` | `/api/admin/document-content/anonymity-statuses/{item_id}` | `legal_text.manage` | LTM |
| `GET` | `/api/admin/users` | `users.manage` or `jurisdiction_admin.assign` or `jurisdiction_super_admin.assign` | JA, JSA, sysadmin |
| `POST` | `/api/admin/assignments` | `users.manage` or `jurisdiction_admin.assign` or `jurisdiction_super_admin.assign` (the rules decide which role) | JA, JSA, sysadmin |
| `DELETE` | `/api/admin/assignments/{assignment_id}` | as `POST /api/admin/assignments` | JA, JSA, sysadmin |
| `POST` | `/api/admin/users/{user_id}/deactivate` | `users.manage` or `accounts.manage` | JA (its jurisdiction's users), JSA (any account) |
| `POST` | `/api/admin/users/{user_id}/reactivate` | `users.manage` or `accounts.manage` | JA (its jurisdiction's users), JSA (any account) |
| `GET` | `/api/admin/role-history` | `role_audit.read` (`user=` narrows it to one user) | JA (its jurisdiction), JSA (all) |
| `GET` | `/api/admin/access-requests` | `access.approve` | JA |
| `POST` | `/api/admin/access-requests/{request_id}/approve` | `access.approve` | JA |
| `POST` | `/api/admin/access-requests/{request_id}/reject` | `access.approve` | JA |
| `GET` | `/api/admin/feature-flags` | `feature_flags.manage` | sysadmin |
| `PUT` | `/api/admin/feature-flags/{name}` | `feature_flags.manage` | sysadmin |
| `GET` | `/api/admin/jurisdictions` | `jurisdictions.manage` | JSA |
| `GET` | `/api/admin/jurisdictions/wording-keys` | `jurisdictions.manage` | JSA |
| `PUT` | `/api/admin/jurisdictions/{code}` | `jurisdictions.manage` (creating, renaming, enabling or disabling, moving between stages, display name and service: and `jurisdiction.create`) | JSA |
| `GET` | `/api/admin/jurisdictions/{code}/templates` | `templates.manage` (DIAAT-344: uploaded Word templates; in the caller's jurisdiction) | JA |
| `POST` | `/api/admin/jurisdictions/{code}/templates` | `templates.manage` | JA |
| `PUT` | `/api/admin/jurisdictions/{code}/templates/{template_id}` | `templates.manage` | JA |
| `GET` | `/api/admin/jurisdictions/{code}/templates/{template_id}/form` | `templates.manage` | JA |
| `POST` | `/api/admin/jurisdictions/{code}/templates/{template_id}/preview` | `templates.manage` | JA |
| `DELETE` | `/api/admin/jurisdictions/{code}/templates/{template_id}` | `templates.manage` | JA |
| `GET` | `/api/admin/jurisdictions/{code}/wording` | `jurisdiction.wording.edit` | JSA |
| `PUT` | `/api/admin/jurisdictions/{code}/wording` | `jurisdiction.wording.edit` | JSA |
| `POST` | `/api/telemetry` | `app.access` | Judge, LTM, JA, JSA |
| `GET` | `/api/jurisdictions/current` | `app.access` | Judge, LTM, JA, JSA |
| `GET` | `/api/jurisdictions/templates` | `recording.own` or `dictation.use` (the uploaded templates a judge can choose) | Judge |
| `POST` | `/api/auth/session` | (exempt: sign_in) | - |
| `GET` | `/api/me/permissions` | (exempt: signed_in) | - |
| `PUT` | `/api/me/context` | (exempt: signed_in) | - |
| `DELETE` | `/api/me/jurisdictions/{code}` | (exempt: signed_in) | - |
| `GET` | `/api/jurisdictions/public` | (exempt: signed_in) | - |
| `GET` | `/api/flags` | (exempt: signed_in) | - |
| `POST` | `/api/access-requests` | (exempt: signed_in) | - |
| `GET` | `/api/access-requests/mine` | (exempt: signed_in) | - |
| `POST` | `/api/access-requests/{request_id}/withdraw` | (exempt: signed_in) | - |
| `GET` | `/api/v1/health` | (exempt: public) | - |
| `GET` | `/api/v1/version` | (exempt: public) | - |
| `POST` | `/api/v1/uploads` | `recording.upload` | Judge |
| `GET` | `/api/v1/local-audio/{blob_name}` | (exempt: signed_url) | - |
| `POST` | `/api/v1/jobs` | `recording.upload` | Judge |
| `GET` | `/api/v1/jobs` | `recording.own` | Judge |
| `GET` | `/api/v1/jobs/{job_id}` | `recording.own` | Judge |
| `POST` | `/api/v1/jobs/{job_id}/baseline` | `recording.own` | Judge |
| `PATCH` | `/api/v1/jobs/{job_id}/segments/{segment}` | `recording.own` | Judge |
| `PATCH` | `/api/v1/jobs/{job_id}/segments/{segment}/words` | `recording.own` | Judge |
| `POST` | `/api/v1/jobs/{job_id}/segments/{segment}/merge-next` | `recording.own` | Judge |
| `POST` | `/api/v1/jobs/{job_id}/segments/{segment}/rollback` | `recording.own` | Judge |
| `POST` | `/api/v1/jobs/{job_id}/segments/{segment}/accept` | `recording.own` | Judge |
| `PATCH` | `/api/v1/jobs/{job_id}/segments/{segment}/speaker` | `recording.own` | Judge |
| `POST` | `/api/v1/jobs/{job_id}/segments/{segment}/history/{history_index}/rollback` | `recording.own` | Judge |
| `GET` | `/api/v1/jobs/{job_id}/history` | `recording.audit.read` | Judge (the owner) |
| `GET` | `/api/v1/jobs/{job_id}/audio` | `recording.own` | Judge |
| `DELETE` | `/api/v1/jobs/{job_id}` | `recording.own` | Judge |
| `GET` | `/health` | (exempt: public) | - |

### The exemptions (the whole list)

| Marker | Routes | Why no permission |
|---|---|---|
| `public_endpoint` | `GET /health`, `/api/health`, `/api/healthcheck`, `/api/health/dependencies`, `/api/v1/health`, `/api/v1/version` | health and version, for probes: no authentication. `/api/health/dependencies` shows names and states only |
| `sign_in_endpoint` | `POST /api/auth/session` | authenticates by the Entra ID token (or, locally, a dev persona) and mints the BenchNotes token |
| `signed_in_endpoint` | `GET /api/me/permissions`, `PUT /api/me/context`, `DELETE /api/me/jurisdictions/{code}`, `GET /api/jurisdictions/public`, `GET /api/flags`, `POST /api/access-requests`, `GET /api/access-requests/mine`, `POST /api/access-requests/{request_id}/withdraw` | any signed-in user, with or without a context or a role (contract section 3): a user with no role is who asks for one |
| `signed_url_endpoint` | `GET /api/v1/local-audio/{blob_name}` | Azure Speech fetches it without our token; see the decision below |

`/api/openapi.json` (FastAPI's own, not an API route) is not guarded by the application; `/docs` is on only in local and dev.

## Judgement calls

* **Dictation routes are `dictation.use`**, every route of `routes_dictation.py` that handles the dictation product: transcriptions and
  everything under them (jobs, minute versions, tags, sectioned transcript, audio), the live transcription, summary, speech token, live
  draft, upload URLs and Langfuse trace and score. With dictation off (DIAAT-388) they all refuse with `dictation_disabled`. That includes
  the **read** routes of records already made (`GET /api/transcriptions-metadata`, `GET /api/transcriptions/{id}`...): there is no permission
  that says "read my old dictation records", and the ticket asks for every dictation route to refuse. The web app's records list uses them,
  so if prod should still list documents while dictation is off, those routes need a decision (a separate permission, or exclude them from the switch).
* **Downloading a document is `documents.export`** (`GET /api/transcriptions/{id}/document-download`, `GET /api/download`): the Judge's permission
  for it, and not behind the dictation switch (the switch only looks at routes whose only way in is `dictation.use`).
* **`GET /api/templates` is `document_content.read`**, not `dictation.use`: it is static metadata of the template categories, used during
  onboarding and the form, so it should not disappear with the dictation switch. Judge and LegalTextManager hold it.
* **`GET /api/document-content` is `document_content.read`** (it was open to any role): the Judge's form content. A LegalTextManager also holds it.
* **`/api/admin/document-content...` is `legal_text.manage`** (read and write alike), which only the LegalTextManager holds. A JurisdictionAdministrator
  could edit legal wording under DIAAT-342 and no longer can (a contract amendment says so); a SystemAdministrator no longer reads it (DIAAT-389).
  This is the legal text (hearing types, legal frameworks, anonymity: the Document Content screen), not the screen wording.
  `document_content.write` is granted to the same role and is not used by a route yet.
* **User, onboarding, profile and telemetry routes are `app.access`**: any role acting in a context. `GET /api/user/onboarding-status` now always says
  `is_allowlisted: true` (the allowlist is the role assignment: a user with none never reaches it); its fields stay for the web app.
* **`GET /api/jurisdictions/current` is `app.access`** and needs a jurisdiction: a JurisdictionSuperAdministrator (global) gets 403 `no_jurisdiction`.
* **Screen wording is the JurisdictionSuperAdministrator's alone (DIAAT-406)**: `GET/PUT /api/admin/jurisdictions/{code}/wording`, from the
  Jurisdictions screen. There is no route for the wording of the jurisdiction the caller acts in (`/api/jurisdictions/wording` was removed).
  Screen wording is the UI labels (`WORDING_KEYS`), not legal text.
* **Deactivation (DIAAT-406)**: `users.manage` (a JurisdictionAdministrator: only users whose roles are all in its jurisdiction, never an
  administrator) or `accounts.manage` (the JurisdictionSuperAdministrator: any account but its own, administrators and users of several
  jurisdictions included). **Role history**: `role_audit.read`, a JurisdictionAdministrator's for its jurisdiction, the
  JurisdictionSuperAdministrator's for all of it (global changes included); `user=` gives one user's changes within that scope.
* **`/api/v1` uploads and job submission are `recording.upload`; everything else on a job is `recording.own`**, and a job is reachable only by its owner (404 as
  for an unknown job). There is no administrator or SystemAdministrator bypass; nothing lists or reads ownerless pre-migration jobs.
* **`GET /api/admin/me` is `admin.access`**; its response is `{email, role, user_id}` where `role` is the caller's own active role (SystemAdministrator for one acting as it);
  `is_admin` is gone.
* **`GET /api/admin/roles` returns `{"roles": [...]}`** (DIAAT-374), the roles the caller may assign by the rules for the role they act as (DIAAT-406; JA: Judge,
  LegalTextManager; JSA: JurisdictionAdministrator only; sysadmin: JurisdictionSuperAdministrator only), never SystemAdministrator. Any of
  `users.manage`, `jurisdiction_admin.assign`, `jurisdiction_super_admin.assign`. A pinned-shape test: `test_privileged_routes.py`.
* **`GET /api/admin/users` is `users.manage`, `jurisdiction_admin.assign` or `jurisdiction_super_admin.assign`** and answers the contract's list (users with their
  assignments, pending rows, paging): a JurisdictionAdministrator lists the jurisdiction it acts in (and, searching with 3 or more characters and no
  `jurisdiction=`/`role=`, any user who has signed in, with only their assignments there, so someone can be added); a JurisdictionSuperAdministrator and a
  SystemAdministrator, who reach it only to appoint, list everyone but see only the assignments of the role they appoint (JurisdictionAdministrator,
  JurisdictionSuperAdministrator); `role=` for another role is 403 `assignment_not_allowed` (DIAAT-406).
  Details and every rule of who may assign what: [`authz-admin-api.md`](authz-admin-api.md). The old-shape `GET /api/admin/users/{id}` and the interim
  `/api/admin/jurisdictions/{code}/members` endpoints (and the `jurisdictions_admin` command line `add-member`/`remove-member`) are gone.
* **`POST/DELETE /api/admin/assignments` accept three permissions**, because which one is enough depends on the role being assigned (Judge and LegalTextManager:
  `users.manage`; JurisdictionAdministrator: `jurisdiction_admin.assign`; JurisdictionSuperAdministrator: `jurisdiction_super_admin.assign`), and that is
  decided per request in `domain/user_admin.py` (403 `assignment_not_allowed`, or `jurisdiction_not_in_scope`), not by the route's declaration.
* **The access request routes for the requester are exempt (`signed_in_endpoint`)**, not `access.request`: a user with no role has no context, and the
  contract says any signed-in user, including one who holds roles and asks for one in another jurisdiction. A caller acting in a context must also hold
  `access.request` to ask (`ensure_permission`, DIAAT-407), so acting as SystemAdministrator it is `permission_denied`; withdrawing and reading their own
  stay open. Approving and listing are `access.approve`:
  only a JurisdictionAdministrator, which sees and decides the requests of the jurisdiction it acts in (DIAAT-406); a JurisdictionSuperAdministrator, and
  one acting as SystemAdministrator, are refused (`permission_denied`); a SystemAdministrator who also holds an approving role has its rights only while acting in it, and with no context gets `context_required`.
* **`GET /api/v1/jobs/{id}/history` is `recording.audit.read`** (Judge), and only the owner reads it: another Judge, or the owner in another jurisdiction, gets the
  404 of a recording they cannot see. A role without the permission (JurisdictionAdministrator, JurisdictionSuperAdministrator, LegalTextManager,
  SystemAdministrator) gets `permission_denied`, as on every other recording route.
* **`GET/PUT /api/admin/jurisdictions/{code}/wording` is `jurisdiction.wording.edit`**: the JurisdictionSuperAdministrator's, for any jurisdiction (DIAAT-406).
  A JurisdictionAdministrator does not hold it.
* **`/api/admin/jurisdictions` and `PUT /api/admin/jurisdictions/{code}` are `jurisdictions.manage`**: the JurisdictionSuperAdministrator's, for any jurisdiction
  (DIAAT-406; a JurisdictionAdministrator and a SystemAdministrator have none). Creating needs `jurisdiction.create` too, and so does changing `name`, `enabled`,
  the stage, display name or service of an existing one (review finding S5); the JurisdictionSuperAdministrator holds both.
* **`/api/admin/jurisdictions/{code}/templates...` (uploaded Word templates, DIAAT-344) is `templates.manage`, scoped**: a JurisdictionAdministrator, in the
  jurisdiction it acts in only (403 `jurisdiction_not_in_scope` for another). The LegalTextManager and the JurisdictionSuperAdministrator no longer hold it (DIAAT-406).
* **A SystemAdministrator acting in a stored role has that role's permissions and scope, and nothing else (DIAAT-403).** The system permissions
  (`jurisdiction_super_admin.assign` among them) are the SystemAdministrator context's alone, and are not read as "global administrator". The user list and
  deactivation take their scope from the role acted in (`jurisdiction_scope`, review finding S4): a JurisdictionAdministrator's is its jurisdiction, and
  nobody else holds `users.manage`; the JurisdictionSuperAdministrator deactivates by `accounts.manage`, which is global (DIAAT-406).
  Acting as SystemAdministrator: assigning JurisdictionSuperAdministrator (the only role it gives, DIAAT-403), listing users to find who to assign, the
  feature flags, the system routes.
* **Giving yourself a role follows the same rules as giving it to anyone else** (DIAAT-406; 403 `assignment_not_allowed` otherwise). A
  JurisdictionSuperAdministrator gives itself JurisdictionAdministrator (any jurisdiction), a JurisdictionAdministrator Judge or LegalTextManager in its
  jurisdiction; a Judge or LegalTextManager raises an access request. Acting as SystemAdministrator, a holder gives JurisdictionSuperAdministrator only,
  to others or to themselves (contract section 1). Nobody removes the role they are acting in. Nobody decides their own access request (403 `own_request`). At most five access requests are pending per user (409 `too_many_pending_requests`).
* **`GET /api/healthcheck/azure-storage` is `system.settings`** (a SystemAdministrator). It was open, called storage on every request and answered with the
  container name and the exception text. Nothing in the repository or the chart calls it; `GET /api/health/dependencies` has the storage state for probes.
* **`POST /api/security/alert-test` is `security.alert_test`** (unchanged in effect: SystemAdministrator, and 404 unless the test is enabled).
* **`GET /api/v1/local-audio/{blob_name}` stays exempt, because its URL is now signed**: `local_storage.build_url` appends `expires` and an HMAC `sig` (key derived from
  `APP_JWT_SIGNING_KEY`, 24 hours), and the route answers 404 for a missing, wrong or expired signature, and for everything unless `AUDIO_STORAGE_BACKEND=local`.
  Before, the unguessable blob name was the only protection. A `sig` with non-ASCII characters is a 404 like any other wrong signature.
* **The upload and job-submission rate limits (`POST /api/v1/uploads` 50/hour, `POST /api/v1/jobs` 100/hour) are per user**: keyed on the `sub` of the verified token,
  not on the token string (a new token every few minutes, and on demand from `PUT /api/me/context`, would otherwise be a new bucket each time). A request with
  no valid token is keyed on its address.
* **The declared permission of every route is pinned** in `tests/unit/permission_matrix_snapshot.json`, compared by `test_permission_matrix.py`: the generated
  matrix trusts what each route declares, so weakening a declaration (say `GET /api/templates` from `document_content.read` to `app.access`) is caught as a diff of
  that file, which a deliberate change regenerates (`UPDATE_PERMISSION_SNAPSHOT=1`) and a reviewer reads.
  The roles that pass each route's check are pinned the same way in `tests/unit/permission_matrix_roles_snapshot.json` (DIAAT-406), so a change to
  the role map shows as a diff of who may call what.
* **`/api/openapi.json`** is FastAPI's document and was already served unauthenticated; left as it is.

## Removed

* `/api/admin/transcriptions`, `/api/admin/transcriptions/{id}`, `/api/admin/transcriptions/{id}/document-download` (DIAAT-389), with their schemas
  and the three data functions behind them. (The provenance of a job's outputs is still stored; nothing serves it. Read it with the query in
  `domain/provenance_dictation.py`.)
* `PUT /api/admin/users/{id}/role` and `DELETE /api/admin/users/{id}` (the Users tab's role change had no lasting effect: DIAAT-375; Phase 4 added the
  assignment endpoints and deactivation), and `admin_delete_user`.
* `_ANY_APP_ROLE`, `_is_admin`, `is_admin`, `_require_role`, `_as_its_admin`, the `get_allowlisted_user(required_roles...)` wrapper, `legacy_roles.py`,
  `approles_*.py`, the role defaults of `hmcts-fastapi-azure-auth` for app roles (including its `Normal`), `AuthenticatedUser.app_roles`, and the
  startup `validate_approles_config` (the `AUTH_APPROLES` setting is gone). The `SystemAdministrator` Entra role is read from the token's `roles` claim by name.

Still there on purpose: `AUTH_ACCESS_ROLES` (the sign-in gate in `token_auth.py`, which uses the library's role check against the Entra `roles` claim; its
removal is a configuration change, Phase 8), the `user.role` column and its check constraint (dropped in a later release), and Word's own "Normal" style in the
document templates.

## Ownerless recording jobs (DIAAT-397 stays open)

Pre-migration jobs with no `user_id` were visible, and changeable, only to a SystemAdministrator. With the bypass gone nobody can list or read them
(`list_jobs_paginated` requires an owner; `_check_job_access` answers 404). The data is untouched. DIAAT-397 decides whether to assign or delete them.

## Dictation off-switch (DIAAT-388)

`DICTATION_ENABLED` (setting, default true) is the default of the flag `dictation_enabled`; a row of `system_feature_flag` with that name overrides it
(`domain/feature_flags.py`, read on every dictation request; if the database cannot be read the default applies). `GET /api/flags` returns
`{"dictation_enabled": bool}` for any signed-in user. The admin endpoints that write the row are `GET/PUT /api/admin/feature-flags` (DIAAT-392, `feature_flags.manage`): each change is audited (`feature_flag.changed`, with
from, to and the roles acted as) and clears the flag's in-process cache. The flag is cached for 5 seconds per process (`feature_flags.CACHE_TTL_SECONDS`), so the
per-request read is one database read per process per 5 seconds; a change made on another replica is seen within that time. Hearing-recording routes do not look at it.

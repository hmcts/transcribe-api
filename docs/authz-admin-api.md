# The administration API (DIAAT-373 Phase 4 and 7)

The endpoints administrators use to give and take away roles, decide access requests, set feature flags and
edit jurisdictions, and the one a Judge uses to read the history of a recording. Every route is behind
`require_permission` (see [`authz-permissions.md`](authz-permissions.md) for the table and
[`authz-token-and-context.md`](authz-token-and-context.md) for the token), and every example below is real
output from `./run-local.sh` with the dev personas (`X-Dev-Persona`), ids shortened.

All under `/api`. Send `Authorization: Bearer <BenchNotes token>`. Errors are `{"detail": {"code", "message"}}`;
a request ID is added next to `detail`. Every change below is audited (`audit_log`): the actor, the jurisdiction
and role they acted in (`active_jurisdiction_id`, `active_role`), the target and the change.

```bash
tok() { curl -s -X POST localhost:8000/api/auth/session -H "X-Dev-Persona: $1" ${2:+-H "X-Requested-Context: $2"} | jq -r .token; }
JADMIN=$(tok jadmin-iac IAC:JurisdictionAdministrator)     # acting as the IAC's administrator
```

## Who may assign what (the rules, in `domain/user_admin.py`)

| Role to assign | Who | Refused with |
|---|---|---|
| `Judge`, `LegalTextManager` in J | a JurisdictionAdministrator acting in J (`users.manage`); a JurisdictionSuperAdministrator in any J | another jurisdiction: 403 `jurisdiction_not_in_scope`; anyone else: 403 `assignment_not_allowed` / `permission_denied` |
| `JurisdictionAdministrator` in J | a JurisdictionSuperAdministrator (`jurisdiction_admin.assign`) | a JurisdictionAdministrator, a SystemAdministrator: 403 `assignment_not_allowed` |
| `JurisdictionSuperAdministrator` (global, no jurisdiction) | a SystemAdministrator only (`jurisdiction_super_admin.assign`) | everyone else: 403 `assignment_not_allowed` |
| `SystemAdministrator` | nobody (it is not a stored role) | 403 `assignment_not_allowed` |
| `JurisdictionSuperAdministrator`, **to themselves** | a SystemAdministrator acting as SystemAdministrator (how the first JurisdictionSuperAdministrator is set up) | |
| a role **to themselves** | everyone else by the rows above, as for anyone: a JurisdictionSuperAdministrator gives themselves JurisdictionAdministrator, Judge or LegalTextManager; a JurisdictionAdministrator Judge or LegalTextManager in its jurisdiction; a Judge or LegalTextManager nothing (they raise an access request) | a role equal to or above the one they act in: 403 `assignment_not_allowed` |
| their own `JurisdictionAdministrator` / `JurisdictionSuperAdministrator` (removing) | nobody but a SystemAdministrator takes away their own administrator role | 403 `assignment_not_allowed` |

The rule (DIAAT-403): nobody gives a role equal to or above the one they act in, to themselves or to anyone else; the one exception is a
SystemAdministrator giving JurisdictionSuperAdministrator, which is the only role a SystemAdministrator gives (or takes away), to others or to
themselves: never JurisdictionAdministrator, Judge or LegalTextManager. Giving a role to yourself follows the same rules as giving it to someone else.

Nothing is assigned into a **disabled** jurisdiction: 409 `jurisdiction_disabled` (also for a SystemAdministrator, and when approving a request for one
that was disabled after the request was made; rejecting still works). An email that matches **more than one** user row (two Entra accounts have signed in with
the same address) is not guessed: 409 `ambiguous_email`, choose the person from the user list and assign by `user_id`.

Removing follows the same rules. Nobody deactivates themselves (403 `cannot_deactivate_self`). A name that is not a
role at all is 400 `invalid_role`. Which of the three permissions the assignment routes accept is enough depends on the
role: the rules above decide per request. The roles a caller may assign are `GET /admin/roles`, and a test asserts
that exactly those are accepted.

## Roles

`GET /api/admin/roles` (`users.manage`, `jurisdiction_admin.assign` or `jurisdiction_super_admin.assign`): the roles the caller may
assign, to others or to themselves, never `SystemAdministrator`. One shape, pinned by a test.

```json
{"roles": ["Judge", "LegalTextManager"]}
```

A JurisdictionSuperAdministrator gets `["Judge", "LegalTextManager", "JurisdictionAdministrator"]`; a SystemAdministrator
`["JurisdictionSuperAdministrator"]`, to others or to themselves.

## Users

`GET /api/admin/users?search=&jurisdiction=&role=&page=1&page_size=25` (`users.manage` or `jurisdiction_super_admin.assign`).
Users who have signed in, ordered by email, then people added by email who have not (`pending: true`). `page_size` is 1 to 200.

* A **JurisdictionAdministrator** lists the users with a role in the jurisdiction it acts in, showing only their assignments there.
  A `search` of three or more characters, with no `jurisdiction=` and no `role=`, matches the email of **any user who has signed in**
  (so someone with no role, or a role elsewhere, can be found and added), again showing only the assignments in its jurisdiction.
  `jurisdiction=` for another jurisdiction is 403 `jurisdiction_not_in_scope`.
* A **JurisdictionSuperAdministrator** or **SystemAdministrator** lists everyone with every assignment (a SystemAdministrator is never
  an assignment and so appears nowhere); `jurisdiction=` and `role=` narrow it to those holding a role there (`role=JurisdictionSuperAdministrator`
  with no jurisdiction lists the global holders).
* The `id` of a `pending` row is its first assignment's id: not a user, so deactivating it is 404, and every `assignments[].id` works with `DELETE /admin/assignments/{id}`.

```bash
curl -s "localhost:8000/api/admin/users?search=walkthrough" -H "Authorization: Bearer $JADMIN"
```
```json
{"items": [{"id": "f2d5a92f-…", "email": "walkthrough.judge@localhost.test", "name": null, "is_active": true,
            "assignments": [{"id": "f2d5a92f-…", "jurisdiction": {"code": "IAC", "name": "Immigration and Asylum Chamber"}, "role": "Judge"}],
            "pending": true}],
 "total": 1, "page": 1, "page_size": 25}
```

## Assignments

`POST /api/admin/assignments` body `{"user_id": uuid | null, "email": str | null, "jurisdiction": code | null, "role": str}` (exactly one of `user_id`
and `email`): **201** with the assignment. An email that belongs to someone who has signed in is assigned to them; any other email makes a
**pending** assignment, claimed at that person's first sign-in (compared case-insensitively). 400 `invalid_target`, `invalid_role`, `invalid_email`,
`invalid_assignment` (a role in a jurisdiction needs one; the global role has none), 403 per the rules, 404 `user_not_found` / `jurisdiction_not_found`,
409 `already_assigned`, 409 `jurisdiction_disabled`, 409 `ambiguous_email`.

```bash
curl -s -X POST localhost:8000/api/admin/assignments -H "Authorization: Bearer $JADMIN" -H 'Content-Type: application/json' \
  -d '{"email":"walkthrough.judge@localhost.test","jurisdiction":"IAC","role":"Judge"}'
```
```json
{"id": "f2d5a92f-…", "jurisdiction": {"code": "IAC", "name": "Immigration and Asylum Chamber"}, "role": "Judge",
 "user_id": null, "email": "walkthrough.judge@localhost.test", "pending": true}
```

The same administrator assigning a role it may not:

```json
{"detail": {"code": "assignment_not_allowed", "message": "Only a JurisdictionSuperAdministrator assigns the JurisdictionAdministrator role."}}
{"detail": {"code": "jurisdiction_not_in_scope", "message": "That jurisdiction is not the one you are working in."}}
```

A SystemAdministrator giving themselves the global role (the first JurisdictionSuperAdministrator of an environment):

```bash
curl -s -X POST localhost:8000/api/admin/assignments -H "Authorization: Bearer $SYS" -H 'Content-Type: application/json' \
  -d '{"user_id":"<their own id>","jurisdiction":null,"role":"JurisdictionSuperAdministrator"}'
```
```json
{"id": "1e37ac18-…", "jurisdiction": null, "role": "JurisdictionSuperAdministrator", "user_id": "c72e94fd-…", "email": "sysadmin@localhost.test", "pending": false}
```

`DELETE /api/admin/assignments/{id}`: **204**. A user's assignment, or a pending one (by the id the user list gave it; the person then gets nothing
at first sign-in). 404 `assignment_not_found`, 403 per the rules.

A role change reaches the person when their token is next minted (within `APP_JWT_TTL_SECONDS`, 300 s by default; at once for a deactivation, which
is checked on every request).

## Deactivating

`POST /api/admin/users/{id}/deactivate` and `/reactivate` (`users.manage`): **204**, idempotent. A deactivated user is refused 403 `user_deactivated`
on their next request and cannot sign in. 404 `user_not_found` (also for a user a JurisdictionAdministrator cannot see). Refused with 403:
`cannot_deactivate_self`; `assignment_not_allowed` for a user who holds a role the caller may not assign (a JurisdictionAdministrator cannot
deactivate another one, and nobody can deactivate a JurisdictionSuperAdministrator: a SystemAdministrator has no `users.manage`), and, for a JurisdictionAdministrator, for a
user who also works in another jurisdiction (deactivation is across all of them: remove their role in yours instead).
The scope is the **role** the caller acts in: a SystemAdministrator who gave themselves a stored role and acts in it has that role's scope (a
JurisdictionAdministrator's: its jurisdiction) and the role's assignment rights, not a global one, so it cannot deactivate a JurisdictionSuperAdministrator or list the
users of another jurisdiction. (Its own permission, `jurisdiction_super_admin.assign`, still lets it list every user, with no context or in a role with no `users.manage`, to find who to make a
JurisdictionSuperAdministrator.)

## Role-change history

`GET /api/admin/role-history?jurisdiction=&page=1&page_size=50` (`role_audit.read`): newest first. A JurisdictionAdministrator reads the
jurisdiction it acts in (`jurisdiction=` for another is 403); a JurisdictionSuperAdministrator everything, or one jurisdiction's with `jurisdiction=`. `change`
is one of `assignment.added`, `assignment.removed`, `user.deactivated`, `user.reactivated`, `membership.leave`, `access_request.approved`,
`access_request.rejected` (an approved request is one row per role).

```json
{"items": [{"id": "…", "timestamp": "2026-11-09T21:27:18Z",
            "actor": {"id": "…", "email": "jadmin-iac@localhost.test"},
            "actor_context": {"jurisdiction": "IAC", "role": "JurisdictionAdministrator"},
            "target": {"id": "…", "email": "no-role@localhost.test"},
            "change": "access_request.approved", "jurisdiction": "IAC", "role": "LegalTextManager"}],
 "total": 7}
```

`actor_context` is null when the actor acted as SystemAdministrator; `target.id` is null, and `target.email` the address,
for a person who has not signed in. (The audit log keeps an email only for such a target: there is no user to name.)

A SystemAdministrator is not named: a change made acting as SystemAdministrator (the audit row carries `by_system`, or that context) shows
`"actor": {"id": null, "email": "system administrator"}` and a null `actor_context` to every reader. One acting in a stored role has only that role's rights
(DIAAT-403), and is stamped with the role and shown as its holder.

Every role change, access request and feature flag change is written to the audit log **in the same transaction** as the change (`audit.stage_audit`,
fail-closed): if the row cannot be written the change is not made (500), so a committed change always has its history row. The audit of everything
else (refusals, recording access, jurisdiction edits) stays fail-open.

## Access requests

For any signed-in user, with or without a role:

* `POST /api/access-requests` body `{"jurisdiction": "IAC", "roles": ["Judge", "LegalTextManager"]}` -> **201** `AccessRequest`
  `{id, jurisdiction: {code, name}, roles, status, created_at, decided_at, reason}`. 400 `role_not_requestable` for anything but Judge and
  LegalTextManager (400 `roles_required` for none), 404 `jurisdiction_not_found` (unknown or disabled), 409 `duplicate_request` (an identical
  pending request), 409 `already_assigned` (they hold a requested role there), 409 `too_many_pending_requests` (five are waiting already: withdraw one). 429 `too_many_requests` (ten made in the last 24 hours, decided or not: asking, withdrawing and asking again would email the approvers each time).
* `GET /api/access-requests/mine` -> every request they made, newest first.
* `POST /api/access-requests/{id}/withdraw` -> the request, `withdrawn`. 404 for someone else's, 409 `request_not_pending` once decided.

For an approver (`access.approve`: a JurisdictionAdministrator of the jurisdiction it acts in, or a JurisdictionSuperAdministrator; a SystemAdministrator is refused):

* `GET /api/admin/access-requests?status=pending` (`pending` by default; also `approved`, `rejected`, `withdrawn`, `all`) -> `AccessRequest` plus
  `"requester": {id, email, name}`, oldest first.
* `POST /api/admin/access-requests/{id}/approve` and `/reject`, body `{"reason": str | null}` (optional, at most 500 characters) -> the request. Approving
  writes the requested assignments **in the same transaction** as the new status (a role already held is left as it is); if that fails the request stays pending.
  403 `jurisdiction_not_in_scope` for another jurisdiction's request, 403 `own_request` for your own (nobody decides their own request: another
  administrator does), 404 `request_not_found`, 409 `request_not_pending` once decided (also if withdrawn), 409 `jurisdiction_disabled` (approving only).

```json
{"id": "febfd968-…", "jurisdiction": {"code": "IAC", "name": "Immigration and Asylum Chamber"}, "roles": ["Judge", "LegalTextManager"],
 "status": "approved", "created_at": "2026-11-09T21:27:17Z", "decided_at": "2026-11-09T21:27:17Z", "reason": "Welcome to the IAC"}
```

The requester's next `POST /api/auth/session` has the new assignments, and `me.pending_requests` (pending ones only) no longer lists it.

### Email (GOV.UK Notify, DIAAT-398)

After the response (never failing or delaying it): a new request emails every active JurisdictionAdministrator of its jurisdiction and every active
JurisdictionSuperAdministrator, once each (not the requester), with `requester_name`, `jurisdiction_name`, `roles` and
`link` = `{APP_URL}/admin?tab=approvals&request=<id>`; an approval or rejection emails the requester `jurisdiction_name`, `roles`, `outcome`
(`approved`/`rejected`) and `link` = `{APP_URL}/`; a withdrawal emails nobody. The reason is not in an email. Settings:
`GOV_NOTIFY_API_KEY` (as the pipeline's), `GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST` and `GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED` (template ids). With no key
nothing is sent and the log says what would have been (no addresses):

```
GOV.UK Notify is not configured: would send email=access_request template=(no template id set) to 4 recipient(s)
```

A send that fails is logged with the exception's type only. Seeding the key in Key Vault and the chart is Phase 8, not done here.

## Feature flags

`GET /api/admin/feature-flags` and `PUT /api/admin/feature-flags/{name}` body `{"value": true|false}` (`feature_flags.manage`: acting as SystemAdministrator
only). Both answer `{"flags": [{name, value, description, updated_at, updated_by}]}`; `updated_by` is the email of whoever set it
last (null while it has its default, which comes from configuration: `DICTATION_ENABLED`). The first flag is `dictation_enabled`. 404 `flag_not_found`;
a value that is not a JSON boolean is 422. Audited as `feature_flag.changed` with `flag`, `from`, `to` and `acted_as`.

```json
{"flags": [{"name": "dictation_enabled", "value": false, "description": "Whether dictation (…) is switched on for every user.",
            "updated_at": "2026-11-09T21:27:30Z", "updated_by": "sysadmin@localhost.test"}]}
```

`GET /api/flags` (any signed-in user, no context needed) then answers `{"dictation_enabled": false}` at once, and every dictation route is 403
`dictation_disabled`. The flag is cached in each API process for 5 seconds (`feature_flags.CACHE_TTL_SECONDS`) so the per-request check is one database
read per process per 5 seconds; the PUT clears the cache of the process that took it, other replicas see it within 5 seconds.

## Jurisdictions

`GET /api/admin/jurisdictions` and `PUT /api/admin/jurisdictions/{code}` keep their shapes (`jurisdictions.manage`, scoped; creating needs
`jurisdiction.create`, a JurisdictionSuperAdministrator's); there is no `auto_enrol` and no members endpoint any more. A JurisdictionAdministrator changes only
the `settings` (template, features, wording) of its own jurisdiction: changing `name` or `enabled` also needs `jurisdiction.create` (403 `permission_denied`; send the
name and `enabled` it already has, or leave `enabled` out). Errors on this endpoint are `{code, message}` like the others (409 `edit_conflict` for a stale `version`,
`already_exists` for adding one that exists). A jurisdiction's wording by code:

* `GET /api/admin/jurisdictions/{code}/wording` -> `{code, name, keys, wording, version}`
* `PUT /api/admin/jurisdictions/{code}/wording` body `{"wording": {key: text}, "version": "<as read>"}` -> the same. 400 for an unknown key, 404 for an unknown
  jurisdiction, 409 if it changed since `version`. `jurisdiction.wording.edit`: a JurisdictionSuperAdministrator edits any jurisdiction's, a
  JurisdictionAdministrator only its own (403 `jurisdiction_not_in_scope`), nobody else (a SystemAdministrator included).

## A recording's history

`GET /api/v1/jobs/{id}/history?page=1&page_size=50` (`recording.audit.read`, the recording's owner acting as a Judge in its jurisdiction): newest first.

```json
{"items": [{"id": "445d58de-…", "timestamp": "2026-11-09T21:27:58Z", "action": "transcript.accept",
            "actor_label": "Judge (IAC)", "actor_role": "Judge", "outcome": "success"}],
 "total": 3}
```

`action` is one of `job.submit`, `transcript.view`, `transcript.edit` (a correction, a merge, a baseline), `transcript.rename_speaker`, `transcript.rollback`,
`transcript.accept`, `audio.export`, `job.delete`. `actor_label` is the owner's name for their own actions; an action by anyone else (a refused attempt) is
"Another user" with no role. **No transcript text** is in the answer or in what is audited (a row holds a kind of edit, a segment id, word positions). Anyone
but the owner gets the 404 of a recording they cannot see (another Judge, or the owner acting in another jurisdiction); a role without the permission gets 403
`permission_denied`, as on every recording route. The history of a deleted recording cannot be read (the recording is gone).

Each new entry of a segment's own correction history (`correction_history` in the stored transcript) now also records `actor_id` and `actor_role`; they are
not sent to the browser.

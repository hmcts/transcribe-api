# The BenchNotes access token, contexts and local personas (DIAAT-373)

Who a user is and what they may do is decided in BenchNotes, not in Entra. Entra proves who someone
is (sign-in) and carries one thing: the `SystemAdministrator` app role. Everything else (which
jurisdictions someone works in, as which role) is a `role_assignment` row (DIAAT-382), and the
user acts in **one (jurisdiction, role) context at a time** (DIAAT-390).

This page is for people building against the API or testing it locally. The permission map is
`domain/auth/permissions.py`; the token is `domain/auth/app_token.py`; sign-in and the session are
`domain/auth/sessions.py` and `api/routes_session.py`.

## The flow

```
browser ── Entra OIDC ──> web (server-side session)
web ── POST /api/auth/session  (Authorization: Bearer <Entra ID token>) ──> API
API ── { token, expires_at, me } ──> web           (token kept in the web session, never in the browser)
web ── any other request  (Authorization: Bearer <BenchNotes token>) ──> API
web ── before expiry: POST /api/auth/session again, with X-Requested-Context
```

* `POST /api/auth/session` is the **only** route that accepts an Entra ID token (or, locally,
  `X-Dev-Persona`). It creates the user at their first sign-in (with no role; what was assigned to their
  email beforehand is theirs), reads their assignments and returns the token.
* **Every other route accepts only the BenchNotes token**, and refuses an Entra token (401). The sign-in
  refuses the BenchNotes token. Health and version endpoints need none.
* Nothing authorises a request by reading the database: the token's signature, issuer, audience and
  expiry are checked, and what it says is trusted until it expires. (The user row is still read, for the
  request's data, and a user deactivated since the token was minted is refused at once: 403
  `user_deactivated`.)

## The token

HS256, signed with `APP_JWT_SIGNING_KEY`. `iss` is `benchnotes-api`, `aud` is `benchnotes`, lifetime
`APP_JWT_TTL_SECONDS` (default 300).

| claim | meaning |
|---|---|
| `sub` | the user's UUID |
| `iat`, `exp` | issued at, expires |
| `sess_exp` | when the Entra-verified sign-in this token descends from stops being good: sign-in time plus one lifetime. Set only at `POST /api/auth/session`; a token minted by `PUT /api/me/context` or `DELETE /api/me/jurisdictions/{code}` carries the presenting token's and expires no later than it (`exp = min(now + TTL, sess_exp)`) |
| `sysadmin` | true if the Entra `roles` claim held `SystemAdministrator` at mint time. Never read from the database |
| `assignments` | `[{"jurisdiction": "ET" \| null, "role": "Judge"}, ...]`, the distinct pairs the user holds |
| `ctx` | `{"jurisdiction": "ET" \| null, "role": "Judge"}`, the pair they act in, or `null` until chosen |
| `name` | display name (informational; never used to authorise) |

**Re-minting never extends the session.** `PUT /api/me/context` and `DELETE /api/me/jurisdictions/{code}` mint from the presenting BenchNotes token,
not from Entra, and copy its `sysadmin` flag. Without a bound a holder could call either every few minutes and keep `sysadmin` for ever after the Entra
role was removed. So the new token never outlives `sess_exp`: a chain of context changes ends one lifetime (300 s) after the last Entra verification, then
the token is 401 as any expired one and the web re-mints from Entra (it already does so within 60 s of expiry and on a 401). The response shapes are
unchanged; `expires_at` of a re-minted token is simply the session's end if that is sooner than a full lifetime. A token without `sess_exp` (minted
before it existed) counts as its own session.

Permissions are **not** in the token: they are derived from `ctx.role` (plus `sysadmin`) with the role
map in code, so a change to the map needs no new tokens. The roles are `Judge`, `LegalTextManager`,
`JurisdictionAdministrator` and `JurisdictionSuperAdministrator` (stored; the last is global, with a null
jurisdiction) and `SystemAdministrator` (Entra only: never stored, never an assignment, never a context).

A `JurisdictionAdministrator` also holds `Judge` and `LegalTextManager` in their own jurisdiction, without being given
them: `domain/assignments.load_assignments` adds them (`derived`) to what a user may act as, so they appear in the token's
`assignments`, in `GET /me/permissions` and in `PUT /me/context`, and are never written to `role_assignment`. Removing
the administrator role (or leaving the jurisdiction) removes them. Not for another jurisdiction, and not for the global
`JurisdictionSuperAdministrator` or the `SystemAdministrator`. When no context is named at sign-in, the user lands in the
one pair they were given (an administrator lands as administrator); two or more given pairs, or none, means they choose.

Settings (`runtime/settings_dictation.py`):

* `APP_JWT_SIGNING_KEY`: required outside `local`, `test` and `preview` (the API refuses to start without it, or
  with fewer than 32 characters; a PR preview makes its own at startup). Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Locally it is in `.env`; if it is
  missing there, local mode signs with a key for that process only (tokens do not survive a restart).
  It is a new Key Vault secret and chart value (not yet added: nothing here touches the cluster config).
* `ENVIRONMENT` is read case-insensitively by the settings and by the persona check (`LOCAL`, `Local` and `local` are one thing). **Unset** counts as
  not local for the signing key: a process whose `ENVIRONMENT` was never set (not in the environment, not in `.env`) must have `APP_JWT_SIGNING_KEY`, whatever
  the settings default says.
* `APP_JWT_TTL_SECONDS`: default 300. A change to someone's assignments, or their removal of their
  active role, reaches them at their next token: within this time, and at once for their own actions
  (choosing a context, leaving a jurisdiction).

## Choosing the context

* Exactly one distinct (jurisdiction, role) pair and the token carries it already: no choice to make.
* Several pairs: `ctx` is `null` and `me.status` is `choose` until `PUT /api/me/context`.
* No pair and not a SystemAdministrator: `no_access`. A SystemAdministrator with no pair is `ready` (the
  system pages only); their system permissions apply whatever the context, also with none.
* On a new token (`POST /api/auth/session`), `X-Requested-Context: <jurisdiction|->:<role>` (`-` for the
  global role) keeps the context the web session chose, **only if the user still holds it**. Otherwise
  `ctx` is `null` and they choose again.
* A route that needs a context, reached with none, is 403 `context_required` (a SystemAdministrator is
  let through to the routes that do not need one).
* Advisory headers: the browser sends `X-Jurisdiction: <code|->` and `X-Role: <role>` for its tab. If
  present and different from the token's `ctx`, the answer is 409 `context_changed` and the tab reloads.
  Absent is fine, and they are not checked on the no-context endpoints.

## Endpoints

All under `/api`. Errors are `{"detail": {"code": ..., "message": ...}}`.

| endpoint | needs a context? | |
|---|---|---|
| `POST /auth/session` | no (Entra token or `X-Dev-Persona`) | `{token, expires_at, me}`. 403 `user_deactivated` |
| `GET /me/permissions` | no | `MeResponse` |
| `PUT /me/context` `{jurisdiction, role}` | no | `{token, expires_at, me}`; 403 `context_not_available` for a pair not held (nothing changes) |
| `DELETE /me/jurisdictions/{code}` | no | leave a jurisdiction: deletes the user's assignments **there only**, audited `membership.leave`; same body as the session. 404 `not_a_member` |
| `GET /jurisdictions/public` | no | `[{code, name, roles}]`, the enabled jurisdictions and the roles that can be requested there (`Judge`, `LegalTextManager`) |
| `GET /jurisdictions/current` | yes | the active jurisdiction's `{code, name, template, features, wording}` (replaces what `GET /jurisdictions/me` gave the web app for its wording and features) |

`MeResponse`:

```json
{
  "user": {"id": "...", "email": "...", "name": "..."},
  "status": "ready | choose | no_access",
  "assignments": [{"jurisdiction": {"code": "ET", "name": "Employment Tribunal", "stage": "live"} | null, "role": "Judge"}],
  "active": {"jurisdiction": {...} | null, "role": "Judge"} | null,
  "permissions": ["app.access", "..."],
  "pending_requests": []
}
```

`GET /api/jurisdictions/me` and `PUT /api/jurisdictions/active` are gone.

## What is audited

Every `audit_log` row written during a request carries `active_jurisdiction_id` and `active_role`: the
context the token acts in (null for a global role's jurisdiction, with no context, and in the worker).
Leaving a jurisdiction is `membership.leave`.

## Local development personas

With `ENVIRONMENT=local` **and a local database** (the host of `DATABASE_CONNECTION_STRING` is `localhost`, `127.0.0.1` or `::1`, the same check as the seed
script), `POST /api/auth/session` accepts `X-Dev-Persona: <name>` instead of an Entra
token, and goes through the same mint as a real sign-in. Anywhere else the header is refused (401
`dev_persona_disabled`), whatever else the request carries.

```bash
docker compose up -d postgres azurite
cp .env.example .env            # add APP_JWT_SIGNING_KEY (see above)
.venv/bin/alembic upgrade head
.venv/bin/python scripts-seed-personas.py    # idempotent; refuses a database that is not your own
./run-local.sh

curl -s -X POST localhost:8000/api/auth/session -H 'X-Dev-Persona: judge-et'
curl -s localhost:8000/api/me/permissions -H "Authorization: Bearer $TOKEN"
```

| name | assignments | sysadmin |
|---|---|---|
| `judge-et` | Judge in ET | no |
| `judge-iac` | Judge in IAC | no |
| `multi` | Judge in ET, JurisdictionAdministrator in IAC | no |
| `jadmin-iac` | JurisdictionAdministrator in IAC | no |
| `jadmin-et` | JurisdictionAdministrator in ET | no |
| `legal-iac` | LegalTextManager in IAC | no |
| `jsuper` | JurisdictionSuperAdministrator (global) | no |
| `sysadmin` | none | yes |
| `no-role` | none | no |

Emails are `<name>@localhost.test`. The seed script creates the jurisdictions ET ("Employment Tribunal")
and IAC if missing, the users and their assignments, and never deletes anything. A persona with
assignments that has not been seeded is 409 `persona_not_seeded`.

Local mode no longer signs every request in as one developer with all roles: without a persona (or a
token) a route is 401, as anywhere else.

## The administration endpoints (Phase 4)

The permission checks on every route are [`authz-permissions.md`](authz-permissions.md) (Phase 3). The endpoints that let administrators
give and take away roles, decide access requests, set feature flags and read a recording's history are
[`authz-admin-api.md`](authz-admin-api.md) (Phase 4), with an example for each. `user.role` and `jurisdiction.auto_enrol` are no longer
read (nobody is enrolled automatically; a new user has no role) and are dropped in a later release; migration 0010 renamed
`jurisdiction_membership` to `jurisdiction_membership_legacy` ([`authz-cutover.md`](authz-cutover.md)).

## Identity: the email claim is not verified (a decision for the owner)

BenchNotes keys two things on the `email` (or `preferred_username`) claim of the Entra token, which Entra does not guarantee unique or verified
(guest or B2B accounts, a tenant where users can edit `mail`):

* **Pending assignments.** An administrator adds `someone@justice.gov.uk` by email; the first account whose token carries that address gets the roles at its
  first sign-in. A second identity with the same address that signs in first takes them, and the real person later signs in with nothing.
* **Assign by email to someone who has already signed in.** The assignment goes to the user row with that address.

What is done (cheap hardening, not a redesign): assigning by an email that matches **more than one** user row is refused (409 `ambiguous_email`: choose the
person from the user list); a pending assignment claimed at a first sign-in when another user row already has the same address is still claimed (first match)
but logged as a warning (no addresses in it). **Residual risk, for the owner to decide:** the address itself is still trusted. Options, none built: require the
`xms_edov` (email domain owner verified) claim or a member (non-guest) account at sign-in; a unique index on `lower(user.email)` with the clash treated as an
error for an administrator to resolve; an administrator confirmation step for pending administrator roles. Until then, the sign-in gate (who may obtain a token for this
app at all) is the control that matters: keep guest accounts out of the app registration's assignment.


## PR previews

A PR preview has no Entra and runs the web app with sign-in off (DIAAT-399), so no Entra token is sent. With
`AUTH_PREVIEW_USER_ENABLED=true` and `ENVIRONMENT=preview` (the API refuses to start with the flag on in any other
environment), `POST /api/auth/session` with **no** Authorization header signs in the fixed preview user
(`domain/auth/token_auth.get_entra_identity`): the roles in `AUTH_PREVIEW_USER_ROLES` (default: every app role, so
the SystemAdministrator flag too), still through the `AUTH_ACCESS_ROLES` check. It is given every role at sign-in
(`domain/auth/sessions.ensure_preview_assignments`): JurisdictionSuperAdministrator, and JurisdictionAdministrator (so
Judge and LegalTextManager too) in every jurisdiction, so it chooses a context like anyone with several, and every
area of the app is reachable. Then it holds a BenchNotes token like anyone else: **every other route still refuses a
request with none** (401), so the web in a preview must sign in through that route (it sends no Authorization header).
A sign-in that does send a token is verified as usual and gets what its user holds. The chart's preview values set
no `APP_JWT_SIGNING_KEY`: a preview signs with a key the API makes for itself at startup (one pod, so one key per preview;
tokens end with the pod, and the web signs the preview user in again). A committed key would let anyone forge a token
for any user of a preview, and previews also take real Entra sign-ins.

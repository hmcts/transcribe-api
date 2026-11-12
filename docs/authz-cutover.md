# Rolling out the new access model (DIAAT-373): cutover

Phase 1 to 4 replace who-may-do-what: roles move from Entra and `user.role` to `role_assignment` rows, a user acts in one
(jurisdiction, role) context, and the API checks permissions, not role names. This page is the order to roll it out in, the
migration that does the cutover, the deploy configuration it needs, and how the first administrator is set up.
Background: [`authz-token-and-context.md`](authz-token-and-context.md), [`authz-permissions.md`](authz-permissions.md),
[`authz-admin-api.md`](authz-admin-api.md).

## Rollout order

1. **Push order (trunk-based: README, "Branches and environments").** There is one long-lived branch, `master`: branch from it, open the PR
   into it, and the API goes first, because a web feature that needs a schema change waits until its additive migration is on transcribe-api
   `master`. Migration **0009** (`0009_role_assignments`, after 0007 jurisdiction stages and 0008 uploaded templates) is additive: it copies
   `jurisdiction_membership` into `role_assignment` once (carrying `tester` over) and changes nothing the previous release reads (it does not set
   `jurisdiction.auto_enrol`: migration 0010 does). It can reach `master` ahead of the code that uses it, or in the same PR as the API.
   The API change is not compatible with the previous web (every route needs a BenchNotes token, which only the new web obtains at sign-in), so
   **the new API and the new web are released together**: merge the API PR, then the web PR, and let both reach an environment before
   anyone signs in there.
2. **While the old code is still serving**, it works on the new schema, with these known limits:
   * an old image started with `RUN_MIGRATIONS=true` **crash-loops** with "Can't locate revision identified by '0009'" (it does not know the revision the
     database is at). Pods that are already running are fine; any old pod that restarts (OOM, node drain, scale-up) or an image rollback hits it. Roll
     back with `alembic downgrade 0008` run from a newer image first, and accept that the new model's data (assignments, requests, flags, names,
     deactivations) is lost by the downgrade; or keep `RUN_MIGRATIONS=false` on old pods;
   * the old `GET /api/admin/users` can answer **500** as soon as a user row has no role (any user the new code created while old pods still serve);
   * the old `DELETE /api/admin/users/{id}` fails (500) for a user who has role assignments (every backfilled user): a foreign key.
   * Memberships the old code writes in this window (an administrator adds a judge; a first sign-in claims a pending membership) reach no assignment:
     that is what migration 0010 repairs.
3. **Configure the deployment** (below) before the new image starts: without `APP_JWT_SIGNING_KEY` it refuses to start outside local.
4. **Cutover**: once the old pods are gone, the release with migration 0010 catches up what they wrote (below). Nothing is run by hand.
5. **Set up the first JurisdictionSuperAdministrator** (below), who sets up everyone else.

## The cutover: migration 0010 (DIAAT-402)

0009 copied the memberships once, when it ran; the previous release's pods went on writing memberships until they were replaced, and
those reached no assignment. Migration **0010** (`0010_authz_cutover`) catches them up. It runs at startup like every migration, so there is
no script to run in any environment. It shipped after the 0009 release had replaced the old pods in AAT, demo and prod, so nothing writes
memberships any more. In one transaction:

1. For every membership with no matching assignment, it adds the assignment with 0009's mapping (`admin` is JurisdictionAdministrator,
   `judge` is Judge): memberships written by the old code after 0009, a membership still pending by email (a pending assignment, the email
   lower case and trimmed, so `New.Judge@Example.com ` and `new.judge@example.com` are one), and a membership the old code claimed for a
   user at their first sign-in (the user's assignment). It then removes the pending assignments 0009 made for a pending membership that the
   old code has since claimed for a user who now holds the role, and puts in the test group the assignments whose membership the old code
   put there after 0009.
2. **It does not give back what was taken away in the new code.** By the time 0010 runs, administrators have been removing roles in the
   new screens, and the memberships those roles came from are still there. Every removal is audited in the same transaction as the change
   (`assignment.removed` by an administrator, by user or pending email; `membership.leave` by the user), filed against the jurisdiction. A
   membership whose person has such an audit row in that jurisdiction is left alone: someone has decided about their access there since 0009.
3. It sets `jurisdiction.auto_enrol` false everywhere and gives the column a default (the code no longer maps it).
4. It renames `jurisdiction_membership` to `jurisdiction_membership_legacy`: nothing reads it, and nothing can grant access from it again.
   The data is kept for recovery.

It never touches users, requests or audit rows, and never removes access (an assignment whose membership the old code deleted stays).
The counts are logged with the migration's own lines at startup ("Running upgrade 0009 -> 0010"):

```
DIAAT-402 cutover: 41 membership(s); assignments added for users: 2; assignments added pending by email: 1; claimed pending assignments
removed: 0; assignments put in the test group: 0; jurisdictions with auto_enrol turned off: 1; left unmatched: 3 whose role was removed
since 0009, 0 other
```

"Other" unmatched memberships (with neither a user nor an email, or anything unexpected) are also logged as a warning, by membership id, to
look at in `jurisdiction_membership_legacy`. It is tested on a real PostgreSQL (`tests/unit/test_migration_0010_cutover.py`).

`jurisdiction.auto_enrol` itself is dropped in a later release, not by 0010: the 0009 release maps it and selects it with every
jurisdiction, so dropping it while its pods may still serve (during the rollout, or after a rollback to it) would fail them.
`alembic downgrade 0009` renames the table back and removes the default; the assignments 0010 added stay.

## Deployment configuration

Only `APP_JWT_SIGNING_KEY` is provisioned by this repository (generated by `infrastructure/secrets.tf`, mounted by the chart); the rest needs its own changes, in the chart or cnp-flux-config.

| What | Value |
|---|---|
| `APP_JWT_SIGNING_KEY` | Key Vault secret `app-jwt-signing-key` (`keyVaults.transcribe.secrets`, alias `APP_JWT_SIGNING_KEY`) in AAT, demo and prod, generated by Terraform (64 characters), **the same value on every replica**, **not PR previews** (a preview makes a key of its own at startup: one pod, so one key, and nothing committed). The API refuses to start without it (or with a short one) outside `local`, `test` and `preview`; an `ENVIRONMENT` that is not set at all counts as deployed. Changing it invalidates every token at once (the web re-mints; about five minutes of 401s at worst). It also signs the local-audio URLs |
| `APP_JWT_TTL_SECONDS` | optional, default 300: how long a role change takes to reach someone (and the longest a `sysadmin` claim outlives its Entra role) |
| `DICTATION_ENABLED=false` | **for prod** (cnp-flux-config): dictation is off at launch (DIAAT-388). A SystemAdministrator turns it on later in the feature flags screen; the flag overrides the setting |
| `AUTH_ACCESS_ROLES` | **emptied** in aat, demo and prod (it still includes `Normal` and the Entra role groups): the sign-in gate must not refuse a user with no role, who must reach "no access yet" and the request form. And `Normal` is dropped from the Entra app registration's roles (DIAAT-386, DIAAT-393). The setting and its check stay in the code until the Entra role groups are retired (Phase 8) |
| Notify | **Not wired yet**: `GOV_NOTIFY_API_KEY` needs the secret `gov-notify-api-key` seeded in each vault and then added to the chart's `keyVaults` (a listed secret that is missing stops the pods starting), plus the two template ids `GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST` ("new access request", to the approvers) and `GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED` ("decided", to the requester). Without a key or a template id nothing is sent and the log says so |
| `APP_URL` | the API's own, for the links in the emails (the worker's is blank; a blank gives a broken `https://` link) |
| Entra | the `SystemAdministrator` app role must reach the API as the `roles` claim with exactly that name (no role map any more) |
| the web | `NEXT_PUBLIC_DICTATION_ENABLED` and the gateway rules for the three token-returning routes: see the web repository |

## The first JurisdictionSuperAdministrator

Nothing is enrolled automatically, so after the cutover only the people carried over from the memberships have a role, and nobody can create the first administrators except a
SystemAdministrator (the Entra app role `SystemAdministrator`, assigned to someone by the Entra administrators, never in BenchNotes).

1. The SystemAdministrator signs in. They have no assignments, so they land on the system pages (`status: ready`, no context): feature flags and the
   Super administrators screen.
2. They give the **JurisdictionSuperAdministrator** role (global, no jurisdiction) to the person who will run access: in the Super administrators screen, by email
   (a pending assignment, claimed at that person's first sign-in: compared without regard to case; refused with `ambiguous_email` if two Entra accounts have
   signed in with that address, then pick the person from the list) or, if the person has signed in, from the user list. The API call is
   `POST /api/admin/assignments {"email": "...", "jurisdiction": null, "role": "JurisdictionSuperAdministrator"}` with a SystemAdministrator's BenchNotes token.
3. A SystemAdministrator may also give **themselves** any stored role (contract section 1): `{"user_id": "<their id, from GET /api/me/permissions>", "jurisdiction": null,
   "role": "JurisdictionSuperAdministrator"}`. Then they sign in again and choose the context. A SystemAdministrator acting in a stored role has **that role's scope**, not a
   global one, and the self-assigned role is subject to the same rules as anyone's (nobody else gives themselves a role; nobody deactivates themselves). They can
   remove it again the same way.
4. The JurisdictionSuperAdministrator then adds the JurisdictionAdministrators of each jurisdiction (by email or from the user list), who add the judges and legal text
   managers and approve access requests. LegalTextManager is **not** carried over from Entra: nobody can edit the legal wording until a JurisdictionAdministrator or a
   JurisdictionSuperAdministrator gives someone LegalTextManager. And Entra-only roles are not carried: check the aat and demo groups before the cutover, since someone in
   the delivery team who held only an Entra role has no assignment afterwards (a Judge membership from 0004 is carried).

Locally the same is done with the dev personas (`docs/authz-token-and-context.md`).

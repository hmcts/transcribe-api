# Rolling out the new access model (DIAAT-373): cutover

Phase 1 to 4 replace who-may-do-what: roles move from Entra and `user.role` to `role_assignment` rows, a user acts in one
(jurisdiction, role) context, and the API checks permissions, not role names. This page is the order to roll it out in, the one
script that has to be run at the cutover, the deploy configuration it needs, and how the first administrator is set up.
Background: [`authz-token-and-context.md`](authz-token-and-context.md), [`authz-permissions.md`](authz-permissions.md),
[`authz-admin-api.md`](authz-admin-api.md).

## Rollout order

1. **Push order (trunk-based: README, "Branches and environments").** There is one long-lived branch, `master`: branch from it, open the PR
   into it, and the API goes first, because a web feature that needs a schema change waits until its additive migration is on transcribe-api
   `master`. Migration **0009** (`0009_role_assignments`, after 0007 jurisdiction stages and 0008 uploaded templates) is additive: it copies
   `jurisdiction_membership` into `role_assignment` once (carrying `tester` over) and changes nothing the previous release reads (it does not set
   `jurisdiction.auto_enrol`: the cutover script does). It can reach `master` ahead of the code that uses it, or in the same PR as the API.
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
     that is what the script below repairs.
3. **Configure the deployment** (below) before the new image starts: without `APP_JWT_SIGNING_KEY` it refuses to start outside local.
4. **Cutover**: once the old pods are gone and the new image is the only one serving, run `scripts-cutover-authz.py`.
5. **Set up the first JurisdictionSuperAdministrator** (below), who sets up everyone else.

## The cutover script: `scripts-cutover-authz.py`

**Run it once, at the cutover.** It is idempotent (it never duplicates and does nothing the second time), but it is not a harmless sync:
**running it again later, after administrators have removed assignments in the new admin screens, adds those assignments back**, because the new code never
changes `jurisdiction_membership` and the script reads the memberships that are still there. It also turns `auto_enrol` off again. After the cutover, do not run it.

What it does, in one transaction:

1. For every membership with no matching assignment, adds the assignment with the migration's mapping (`admin` is JurisdictionAdministrator, `judge` is
   Judge): memberships written by the old code after 0009, a membership still pending by email (pending assignment, email lower case and trimmed, so
   `New.Judge@Example.com ` and `new.judge@example.com` are one), and a membership the old code claimed for a user at their first sign-in (the user's
   assignment). Then removes the pending assignments the migration made for a pending membership that the old code has since claimed for a user who now holds the role.
2. Sets `jurisdiction.auto_enrol` false everywhere.

It never removes access otherwise (an assignment whose membership the old code deleted in the window stays; the script cannot tell it from one made in the new
screens), and never touches users, requests or audit rows.

```bash
export DATABASE_CONNECTION_STRING=postgresql://...           # the environment's database; needs only the URL, not the API settings
.venv/bin/python scripts-cutover-authz.py                    # DRY RUN: the default, writes nothing, prints the counts
.venv/bin/python scripts-cutover-authz.py --apply            # at the cutover: commits
```

The dry run does the work in a transaction it rolls back, so its counts are what `--apply` will do:

```
database: postgresql://app:***@host/benchnotes
mode: DRY RUN (nothing written; rerun with --apply at cutover)
memberships (jurisdiction_membership):             41
assignments before:                                 38
assignments would add for users:                     2
assignments would add pending by email:              1
redundant pending assignments to remove:            0
jurisdictions with auto_enrol turned off (would be): 1
memberships still without an assignment:            0  (0 expected)
assignments after:                                  41
```

"Memberships still without an assignment" must be 0 after `--apply`. Add the dry run to the DIAAT-387 checklist a day before and the `--apply`
to the cutover itself. It is tested on a real PostgreSQL (`tests/unit/test_cutover_script.py`): dry run writes nothing, apply maps as above, a second run changes
nothing, mixed-case and padded emails give one assignment, and what the migration already copied is not copied again.

It is not in the container image (only `src/` is). Run it from a checkout of the release, on a machine that can reach the database, or copy it into a pod with the
database URL in its environment.

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

Nothing is enrolled automatically, so after the cutover only the people the script carried over have a role, and nobody can create the first administrators except a
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

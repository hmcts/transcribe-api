"""The DIAAT-373 cutover, as a migration (DIAAT-402), and the retirement of the old access data.

0009 copied ``jurisdiction_membership`` into ``role_assignment`` once, when it ran. Pods still on the
release before it went on writing memberships until they were replaced (an administrator added a judge
or an administrator, changed a role or the test group; a first sign-in claimed a pending membership or
auto-enrolled the user in the IAC), and none of those reached an assignment. Every environment now runs
the 0009 release only, so nothing writes memberships any more and this catches up the gap once.

Each membership is first resolved to its person: its user, or for one pending by email (lower case and
trimmed, as domain/assignments.normalise_email stores it) the one user who has since signed in with that
email, else the email itself. A pending membership whose email belongs to more than one user is left
alone (the new code refuses such an email too: ``ambiguous_email``), and a blank one matches nobody.
The memberships are resolved once, into a temporary table every statement reads, under locks that hold
sign-ins and role changes on pods still serving until this commits. Then:

1. 0009's pending copies are trimmed (it only lowered them, so a padded one could never be claimed); two
   that become the same are one, in the test group if either was. A padded copy whose email had a role
   removed in its jurisdiction since 0009 is dropped instead, so trimming cannot give it back.
2. An assignment for every person who holds none for their membership, with 0009's mapping (``admin`` is
   JurisdictionAdministrator, anything else Judge): to the user, or pending by email. Memberships of the
   same person are one assignment, in the test group if any of them was.
3. The pending assignment 0009 copied for a person who is now a user holding the role (the old release
   claimed the membership; the new code claims pending assignments only at a first sign-in).
4. The test group flag of a membership the old release put in the group after 0009 copied it.

A membership with no assignment is not always the gap: administrators have been removing roles in the
new screens since 0009, and those memberships are still there. Every removal is audited in the same
transaction as the change (``assignment.removed`` by an administrator, naming the user or the pending
email; ``membership.leave`` by the user; ``audit.stage_audit`` is fail-closed), filed against the
jurisdiction. Steps 2 and 4 leave alone any person with such an audit row in that jurisdiction, by
user or by email: someone has decided about their access there since, and the old data must not
override it. The counts go to the log, with what is left unmatched.

Then the old data is retired, so nothing can grant access from it again:

- ``jurisdiction_membership`` becomes ``jurisdiction_membership_legacy``: kept (recoverable), not read.
- ``jurisdiction.auto_enrol`` is set false everywhere and given a default, so the code, which no longer
  maps it, can insert jurisdictions. The column itself stays until a later release: the 0009 release maps
  it and selects it with every jurisdiction, so dropping it here would fail those pods for as long as they
  serve during this rollout, and any rollback to them.

Downgrade restores the table name and removes the default. The assignments it added and the pending
copies it removed or trimmed stay: they are what the 0009 release expects to find. auto_enrol stays
false: the 0009 release never reads it.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12
"""

import logging

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Alembic's own logger: the counts appear next to "Running upgrade 0009 -> 0010" in the startup log.
logger = logging.getLogger("alembic.runtime.migration")

LEGACY_TABLE = "jurisdiction_membership_legacy"

# Sign-ins and role changes on pods still serving wait until this commits: a user created, or a role
# removed, between two of the statements below would otherwise be missed by both. "user" first, as the
# sign-in takes them, so the two cannot deadlock.
LOCKS = (
    'LOCK TABLE "user" IN SHARE ROW EXCLUSIVE MODE',
    "LOCK TABLE role_assignment IN SHARE ROW EXCLUSIVE MODE",
)

# Every membership as (person, jurisdiction, role), resolved once: user_id is its user, or the only user
# with its pending email; email is the person's, normalised; ambiguous: a pending email several users have.
RESOLVE = """
CREATE TEMPORARY TABLE cutover_legacy ON COMMIT DROP AS
SELECT m.id, m.jurisdiction_id, m.tester, m.created_datetime,
       CASE m.role WHEN 'admin' THEN 'JurisdictionAdministrator' ELSE 'Judge' END AS role,
       coalesce(m.user_id, CASE WHEN s.users = 1 THEN s.user_id END) AS user_id,
       lower(btrim(coalesce(u.email, m.email))) AS email,
       m.user_id IS NULL AND s.users > 1 AS ambiguous
FROM jurisdiction_membership m
LEFT JOIN "user" u ON u.id = m.user_id
CROSS JOIN LATERAL (
  SELECT count(*) AS users, (array_agg(o.id))[1] AS user_id FROM "user" o
  WHERE m.user_id IS NULL AND btrim(m.email) <> '' AND lower(btrim(o.email)) = lower(btrim(m.email))
) s
"""

# Someone removed a role of this person in this jurisdiction after 0009 (user_admin.delete_assignment,
# routes_session.leave). 0009's copy of a pending membership was pending, so its removal names the email.
_DECIDED = """EXISTS (
    SELECT 1 FROM audit_log e
    WHERE e.object_id = l.jurisdiction_id::text
      AND (
        (e.event_type = 'assignment.removed'
         AND (e.detail->>'target_user_id' = l.user_id::text OR lower(btrim(e.detail->>'target_email')) = l.email))
        OR (e.event_type = 'membership.leave' AND e.user_id = l.user_id::text)
      )
  )"""

_HELD = """EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.jurisdiction_id = l.jurisdiction_id AND a.role = l.role
      AND CASE WHEN l.user_id IS NOT NULL THEN a.user_id = l.user_id
          ELSE a.user_id IS NULL AND a.pending_email = l.email END
  )"""

_GRANTABLE = f"NOT l.ambiguous AND (l.user_id IS NOT NULL OR l.email <> '') AND NOT {_HELD} AND NOT {_DECIDED}"

_PADDED = "p.user_id IS NULL AND p.pending_email <> btrim(p.pending_email)"

# A padded copy could never be claimed; once trimmed it could. One whose email had a role removed in its
# jurisdiction since 0009 (its unpadded twin's, say) is dropped instead, so trimming gives nothing back.
DELETE_PADDED_REMOVED = f"""
DELETE FROM role_assignment p
WHERE {_PADDED}
  AND EXISTS (
    SELECT 1 FROM audit_log e
    WHERE e.event_type = 'assignment.removed' AND e.object_id = p.jurisdiction_id::text
      AND lower(btrim(e.detail->>'target_email')) = btrim(p.pending_email)
  )
"""

MERGE_PADDED_TESTER = f"""
UPDATE role_assignment k SET tester = true
FROM role_assignment p
WHERE {_PADDED} AND p.tester
  AND k.user_id IS NULL AND k.id <> p.id AND NOT k.tester AND btrim(k.pending_email) = btrim(p.pending_email)
  AND k.jurisdiction_id IS NOT DISTINCT FROM p.jurisdiction_id AND k.role = p.role
"""

DELETE_PADDED_DUPLICATES = f"""
DELETE FROM role_assignment p
WHERE {_PADDED}
  AND EXISTS (
    SELECT 1 FROM role_assignment k
    WHERE k.user_id IS NULL AND k.id <> p.id AND btrim(k.pending_email) = btrim(p.pending_email)
      AND k.jurisdiction_id IS NOT DISTINCT FROM p.jurisdiction_id AND k.role = p.role
      AND (k.pending_email = btrim(k.pending_email) OR k.id < p.id)
  )
"""

TRIM_PENDING = f"UPDATE role_assignment p SET pending_email = btrim(p.pending_email) WHERE {_PADDED}"

ADD_FOR_USERS = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)
SELECT gen_random_uuid(), l.user_id, NULL, l.jurisdiction_id, l.role, NULL, min(l.created_datetime), bool_or(l.tester)
FROM cutover_legacy l
WHERE l.user_id IS NOT NULL AND {_GRANTABLE}
GROUP BY l.user_id, l.jurisdiction_id, l.role
ON CONFLICT DO NOTHING
"""

ADD_PENDING = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)
SELECT gen_random_uuid(), NULL, l.email, l.jurisdiction_id, l.role, NULL, min(l.created_datetime), bool_or(l.tester)
FROM cutover_legacy l
WHERE l.user_id IS NULL AND l.email IS NOT NULL AND {_GRANTABLE}
GROUP BY l.email, l.jurisdiction_id, l.role
ON CONFLICT DO NOTHING
"""

REMOVE_CLAIMED_PENDING = """
DELETE FROM role_assignment p
USING cutover_legacy l
WHERE p.user_id IS NULL AND l.user_id IS NOT NULL AND NOT l.ambiguous
  AND p.pending_email = l.email AND p.jurisdiction_id = l.jurisdiction_id AND p.role = l.role
  AND EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.user_id = l.user_id AND a.jurisdiction_id = l.jurisdiction_id AND a.role = l.role
  )
"""

SYNC_TESTER = f"""
UPDATE role_assignment a SET tester = true
FROM cutover_legacy l
WHERE l.tester AND NOT a.tester AND NOT l.ambiguous
  AND a.jurisdiction_id = l.jurisdiction_id AND a.role = l.role
  AND CASE WHEN l.user_id IS NOT NULL THEN a.user_id = l.user_id
      ELSE a.user_id IS NULL AND a.pending_email = l.email END
  AND NOT {_DECIDED}
"""

UNMATCHED = f"SELECT l.id, {_DECIDED} FROM cutover_legacy l WHERE NOT {_HELD} ORDER BY l.id"

AUTO_ENROL_OFF = "UPDATE jurisdiction SET auto_enrol = false WHERE auto_enrol"

CATCH_UP = (
    ("padded pending assignments removed since 0009 dropped", DELETE_PADDED_REMOVED),
    ("padded pending assignments merged", MERGE_PADDED_TESTER),
    ("padded pending duplicates removed", DELETE_PADDED_DUPLICATES),
    ("pending emails trimmed", TRIM_PENDING),
    ("assignments added for users", ADD_FOR_USERS),
    ("assignments added pending by email", ADD_PENDING),
    ("claimed pending assignments removed", REMOVE_CLAIMED_PENDING),
    ("assignments put in the test group", SYNC_TESTER),
    ("jurisdictions with auto_enrol turned off", AUTO_ENROL_OFF),
)


def upgrade() -> None:
    for statement in (*LOCKS, RESOLVE):
        op.execute(sa.text(statement))
    if context.is_offline_mode():
        for _name, statement in CATCH_UP:
            op.execute(sa.text(statement))
    else:
        bind = op.get_bind()
        memberships = bind.execute(sa.text("SELECT count(*) FROM jurisdiction_membership")).scalar_one()
        counts = [(name, bind.execute(sa.text(statement)).rowcount) for name, statement in CATCH_UP]
        unmatched = bind.execute(sa.text(UNMATCHED)).all()
        decided = [str(row[0]) for row in unmatched if row[1]]
        other = [str(row[0]) for row in unmatched if not row[1]]
        logger.info(
            "DIAAT-402 cutover: %d membership(s); %s; left unmatched: %d whose role was removed since 0009, %d other",
            memberships,
            "; ".join(f"{name}: {count}" for name, count in counts),
            len(decided),
            len(other),
        )
        if other:
            logger.warning("DIAAT-402 cutover: memberships with no assignment (ids): %s", ", ".join(other))
    op.execute(sa.text("DROP TABLE cutover_legacy"))

    op.alter_column("jurisdiction", "auto_enrol", existing_type=sa.Boolean(), server_default=sa.false())
    op.rename_table("jurisdiction_membership", LEGACY_TABLE)


def downgrade() -> None:
    op.rename_table(LEGACY_TABLE, "jurisdiction_membership")
    op.alter_column("jurisdiction", "auto_enrol", existing_type=sa.Boolean(), server_default=None)

"""The DIAAT-373 cutover, as a migration (DIAAT-402), and the retirement of the old access data.

0009 copied ``jurisdiction_membership`` into ``role_assignment`` once, when it ran. Pods still on the
release before it went on writing memberships until they were replaced (an administrator added a judge
or an administrator, changed a role or the test group; a first sign-in claimed a pending membership or
auto-enrolled the user in the IAC), and none of those reached an assignment. Every environment now runs
the 0009 release only, so nothing writes memberships any more and this catches up the gap once:

1. An assignment for every membership that has none, with 0009's mapping (``admin`` is
   JurisdictionAdministrator, anything else Judge; a membership pending by email stays pending, the
   email lower case and trimmed). Pending memberships whose emails differ only in case or padding are
   one assignment, in the test group if any of them was.
2. The pending assignment 0009 copied from a pending membership that the old release then claimed for
   a user, once that user holds the role (the new code claims pending assignments only at a user's
   first sign-in, which has happened).
3. The test group flag of a membership the old release put in the group after 0009 copied it.

A membership with no assignment is not always the gap: administrators have been removing roles in the
new screens since 0009, and those memberships are still there. Every removal is audited in the same
transaction as the change (``assignment.removed`` by an administrator, ``membership.leave`` by the user;
``audit.stage_audit`` is fail-closed), filed against the jurisdiction. Steps 1 and 3 leave alone any
membership whose person has such an audit row in that jurisdiction: someone has decided about their
access there since, and the old data must not override it. The counts go to the log, with what is left
unmatched.

Then the old data is retired, so nothing can grant access from it again:

- ``jurisdiction_membership`` becomes ``jurisdiction_membership_legacy``: kept (recoverable), not read.
- ``jurisdiction.auto_enrol`` is set false everywhere and given a default, so the code, which no longer
  maps it, can insert jurisdictions. The column itself stays until a later release: the 0009 release maps
  it and selects it with every jurisdiction, so dropping it here would fail those pods for as long as they
  serve during this rollout, and any rollback to them.

Downgrade restores the table name and removes the default. The assignments it added and the pending
copies it removed stay: they are what the 0009 release expects to find. auto_enrol stays false: the 0009
release never reads it.

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

_ROLE = "CASE m.role WHEN 'admin' THEN 'JurisdictionAdministrator' ELSE 'Judge' END"
# What a pending email is stored and matched as (domain/assignments.normalise_email).
_PENDING_EMAIL = "lower(btrim(m.email))"

# Someone removed a role of this member in this jurisdiction after 0009 (user_admin.delete_assignment,
# routes_session.leave). A claimed membership's 0009 copy was pending, so its removal names the email.
_DECIDED_FOR_USER = """EXISTS (
    SELECT 1 FROM audit_log l JOIN "user" u ON u.id = m.user_id
    WHERE l.object_id = m.jurisdiction_id::text
      AND (
        (l.event_type = 'assignment.removed'
         AND (l.detail->>'target_user_id' = u.id::text OR l.detail->>'target_email' = lower(btrim(u.email))))
        OR (l.event_type = 'membership.leave' AND l.user_id = u.id::text)
      )
  )"""
_DECIDED_FOR_EMAIL = f"""EXISTS (
    SELECT 1 FROM audit_log l
    WHERE l.object_id = m.jurisdiction_id::text
      AND l.event_type = 'assignment.removed' AND l.detail->>'target_email' = {_PENDING_EMAIL}
  )"""

_HAS_USER_ASSIGNMENT = f"""EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.user_id = m.user_id AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}
  )"""
_HAS_PENDING_ASSIGNMENT = f"""EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.user_id IS NULL AND a.pending_email = {_PENDING_EMAIL}
      AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}
  )"""

ADD_FOR_USERS = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)
SELECT gen_random_uuid(), m.user_id, NULL, m.jurisdiction_id, {_ROLE}, NULL, m.created_datetime, m.tester
FROM jurisdiction_membership m
WHERE m.user_id IS NOT NULL AND NOT {_HAS_USER_ASSIGNMENT} AND NOT {_DECIDED_FOR_USER}
ON CONFLICT DO NOTHING
"""

ADD_PENDING = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)
SELECT gen_random_uuid(), NULL, s.email, s.jurisdiction_id, s.role, NULL, s.created, s.tester
FROM (
  SELECT {_PENDING_EMAIL} AS email, m.jurisdiction_id, {_ROLE} AS role,
         min(m.created_datetime) AS created, bool_or(m.tester) AS tester
  FROM jurisdiction_membership m
  WHERE m.user_id IS NULL AND m.email IS NOT NULL AND btrim(m.email) <> ''
    AND NOT {_HAS_PENDING_ASSIGNMENT} AND NOT {_DECIDED_FOR_EMAIL}
  GROUP BY {_PENDING_EMAIL}, m.jurisdiction_id, {_ROLE}
) s
ON CONFLICT DO NOTHING
"""

REMOVE_CLAIMED_PENDING = f"""
DELETE FROM role_assignment p
USING "user" u, jurisdiction_membership m
WHERE p.user_id IS NULL AND p.pending_email IS NOT NULL
  AND lower(btrim(u.email)) = p.pending_email
  AND m.user_id = u.id AND m.jurisdiction_id = p.jurisdiction_id AND p.role = {_ROLE}
  AND EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.user_id = u.id AND a.jurisdiction_id = p.jurisdiction_id AND a.role = p.role
  )
"""

SYNC_TESTER = f"""
UPDATE role_assignment a SET tester = true
FROM jurisdiction_membership m
WHERE m.tester AND NOT a.tester
  AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}
  AND (
    (a.user_id IS NOT NULL AND a.user_id = m.user_id AND NOT {_DECIDED_FOR_USER})
    OR (a.user_id IS NULL AND m.user_id IS NULL AND a.pending_email = {_PENDING_EMAIL} AND NOT {_DECIDED_FOR_EMAIL})
  )
"""

UNMATCHED = f"""
SELECT m.id, CASE WHEN m.user_id IS NOT NULL THEN {_DECIDED_FOR_USER} ELSE {_DECIDED_FOR_EMAIL} END
FROM jurisdiction_membership m
WHERE CASE WHEN m.user_id IS NOT NULL THEN NOT {_HAS_USER_ASSIGNMENT}
      ELSE NOT {_HAS_PENDING_ASSIGNMENT} END
ORDER BY m.id
"""

AUTO_ENROL_OFF = "UPDATE jurisdiction SET auto_enrol = false WHERE auto_enrol"

CATCH_UP = (
    ("assignments added for users", ADD_FOR_USERS),
    ("assignments added pending by email", ADD_PENDING),
    ("claimed pending assignments removed", REMOVE_CLAIMED_PENDING),
    ("assignments put in the test group", SYNC_TESTER),
    ("jurisdictions with auto_enrol turned off", AUTO_ENROL_OFF),
)


def upgrade() -> None:
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

    op.alter_column("jurisdiction", "auto_enrol", existing_type=sa.Boolean(), server_default=sa.false())
    op.rename_table("jurisdiction_membership", LEGACY_TABLE)


def downgrade() -> None:
    op.rename_table(LEGACY_TABLE, "jurisdiction_membership")
    op.alter_column("jurisdiction", "auto_enrol", existing_type=sa.Boolean(), server_default=None)

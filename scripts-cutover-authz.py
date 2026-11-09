#!/usr/bin/env python3
"""Cutover for the DIAAT-373 access model: run ONCE, when the new release goes live.

*** RUN THIS ONCE, AT CUTOVER. ***  It is safe to run again (it never duplicates and never fails
on what is already done), but running it AGAIN LATER, after administrators have started removing
role assignments in the new admin screens, WILL ADD BACK every assignment they removed whose
membership is still in ``jurisdiction_membership`` (the new code never changes that table), and
will turn ``auto_enrol`` off again if someone turned it on. After the cutover, do not run it.

Why it exists (review findings MIG-1 and MIG-2). Migration 0007 copies ``jurisdiction_membership``
into ``role_assignment`` once, when it runs. It ships on ``master`` first and the new code follows
later, and in between the previous release keeps writing memberships (an administrator adds a judge;
a first sign-in claims a pending membership) that reach no assignment, so those people would have no
role the day the new code goes live. And the previous release still reads ``jurisdiction.auto_enrol``,
so 0007 does not change it. This script does both at the cutover, when the old pods are gone:

1. For every membership with no matching assignment, add the assignment, with the migration's mapping:
   ``admin`` is JurisdictionAdministrator, ``judge`` is Judge, a membership still pending by email stays
   pending (the email in lower case and trimmed, claimed at first sign-in), a membership the previous
   release claimed for a user in the meantime becomes that user's assignment. Then remove the pending
   assignments that the first step has made redundant (the migration's copy of a pending membership
   the previous release has since claimed for a user who now holds the role).
2. Set ``jurisdiction.auto_enrol`` false everywhere (nobody is enrolled automatically any more; the
   column stays until a later release drops it).

It writes nothing unless ``--apply`` is given: the default is a DRY RUN, which does the work in a
transaction it rolls back and prints what it would do. Counts are printed either way.

    DATABASE_CONNECTION_STRING=postgresql://... python scripts-cutover-authz.py            # dry run
    DATABASE_CONNECTION_STRING=postgresql://... python scripts-cutover-authz.py --apply    # at cutover

It needs only the database URL (not the API's settings), so it can be run from a machine that can reach
the database. It does not touch users, access requests, audit rows or anything else, and it does not
remove assignments (a membership the previous release deleted in the window leaves its assignment; the
report cannot tell it from one made in the new screens). See docs/authz-cutover.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

# The migration's mapping (alembic/versions/0007_role_assignments.py): admin -> JurisdictionAdministrator,
# anything else (judge) -> Judge.
_ROLE = "CASE m.role WHEN 'admin' THEN 'JurisdictionAdministrator' ELSE 'Judge' END"
# What a pending email is stored and matched as everywhere else (domain/assignments.normalise_email).
_PENDING_EMAIL = "lower(btrim(m.email))"

_ADD_FOR_USERS = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at)
SELECT gen_random_uuid(), m.user_id, NULL, m.jurisdiction_id, {_ROLE}, NULL, m.created_datetime
FROM jurisdiction_membership m
WHERE m.user_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM role_assignment a
    WHERE a.user_id = m.user_id AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}
  )
ON CONFLICT DO NOTHING
"""

# DISTINCT ON: two pending memberships whose emails differ only in case or padding are one assignment.
_ADD_PENDING = f"""
INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at)
SELECT gen_random_uuid(), NULL, s.email, s.jurisdiction_id, s.role, NULL, s.created
FROM (
  SELECT DISTINCT ON ({_PENDING_EMAIL}, m.jurisdiction_id, {_ROLE})
         {_PENDING_EMAIL} AS email, m.jurisdiction_id, {_ROLE} AS role, m.created_datetime AS created
  FROM jurisdiction_membership m
  WHERE m.user_id IS NULL AND m.email IS NOT NULL AND btrim(m.email) <> ''
    AND NOT EXISTS (
      SELECT 1 FROM role_assignment a
      WHERE a.user_id IS NULL AND a.pending_email = {_PENDING_EMAIL}
        AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}
    )
  ORDER BY {_PENDING_EMAIL}, m.jurisdiction_id, {_ROLE}, m.created_datetime
) s
ON CONFLICT DO NOTHING
"""

# A pending assignment (the migration's copy of a pending membership) whose membership the previous
# release has since claimed for a user: the user holds the role now (the first statement), so the
# pending row is redundant and would only show as a pending email in the user list.
_REMOVE_CLAIMED_PENDING = f"""
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

_AUTO_ENROL_OFF = "UPDATE jurisdiction SET auto_enrol = false WHERE auto_enrol"

_STILL_UNMATCHED = f"""
SELECT count(*) FROM jurisdiction_membership m
WHERE CASE WHEN m.user_id IS NOT NULL THEN NOT EXISTS (
        SELECT 1 FROM role_assignment a
        WHERE a.user_id = m.user_id AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE})
      ELSE m.email IS NOT NULL AND btrim(m.email) <> '' AND NOT EXISTS (
        SELECT 1 FROM role_assignment a
        WHERE a.user_id IS NULL AND a.pending_email = {_PENDING_EMAIL}
          AND a.jurisdiction_id = m.jurisdiction_id AND a.role = {_ROLE}) END
"""


@dataclass(frozen=True)
class Report:
    memberships: int
    assignments_before: int
    added_for_users: int
    added_pending: int
    removed_claimed_pending: int
    auto_enrol_turned_off: int
    still_unmatched: int
    assignments_after: int


def run(connection: Connection) -> Report:
    """The whole cutover on ``connection``'s transaction (the caller commits or rolls back)."""
    if not connection.execute(text("SELECT to_regclass('role_assignment') IS NOT NULL")).scalar_one():
        msg = "role_assignment does not exist: run the migrations (alembic upgrade head) first"
        raise RuntimeError(msg)

    def count(sql: str) -> int:
        return connection.execute(text(sql)).scalar_one()

    memberships = count("SELECT count(*) FROM jurisdiction_membership")
    before = count("SELECT count(*) FROM role_assignment")
    added_for_users = connection.execute(text(_ADD_FOR_USERS)).rowcount
    added_pending = connection.execute(text(_ADD_PENDING)).rowcount
    removed = connection.execute(text(_REMOVE_CLAIMED_PENDING)).rowcount
    auto_enrol = connection.execute(text(_AUTO_ENROL_OFF)).rowcount
    return Report(
        memberships=memberships,
        assignments_before=before,
        added_for_users=added_for_users,
        added_pending=added_pending,
        removed_claimed_pending=removed,
        auto_enrol_turned_off=auto_enrol,
        still_unmatched=count(_STILL_UNMATCHED),
        assignments_after=count("SELECT count(*) FROM role_assignment"),
    )


def _print(report: Report, *, apply: bool, target: str) -> None:
    verb = "added" if apply else "would add"
    print(f"database: {target}")
    print(f"mode: {'APPLY (committed)' if apply else 'DRY RUN (nothing written; rerun with --apply at cutover)'}")
    print(f"memberships (jurisdiction_membership):             {report.memberships}")
    print(f"assignments before:                                 {report.assignments_before}")
    print(f"assignments {verb} for users:                     {report.added_for_users}")
    print(f"assignments {verb} pending by email:              {report.added_pending}")
    print(f"redundant pending assignments {'removed' if apply else 'to remove'}:            {report.removed_claimed_pending}")
    print(f"jurisdictions with auto_enrol turned {'off' if apply else 'off (would be)'}:     {report.auto_enrol_turned_off}")
    print(f"memberships still without an assignment:            {report.still_unmatched}  (0 expected)")
    print(f"assignments after:                                  {report.assignments_after}")
    if apply:
        print("Done. Do not run this again after administrators have changed assignments: it would add them back.")


def main(argv: list[str] | None = None, engine: Engine | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts-cutover-authz.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (the default is a dry run)")
    args = parser.parse_args(argv)

    if engine is None:
        url = os.environ.get("DATABASE_CONNECTION_STRING")
        if not url:
            print("DATABASE_CONNECTION_STRING is not set.")
            return 2
        engine = create_engine(url)
    target = make_url(str(engine.url)).render_as_string(hide_password=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            report = run(connection)
        except Exception as exc:
            transaction.rollback()
            print(f"Failed, nothing written: {type(exc).__name__}: {exc}")
            return 1
        if args.apply:
            transaction.commit()
        else:
            transaction.rollback()
    _print(report, apply=args.apply, target=target)
    return 0


if __name__ == "__main__":
    sys.exit(main())

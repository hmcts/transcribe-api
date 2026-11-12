"""Database-backed role assignments, access requests and feature flags (DIAAT-373).

A user's roles move from the Entra token and ``user.role`` to the database, as
(user, jurisdiction, role) rows, with the active (jurisdiction, role) chosen per
session (DIAAT-382, DIAAT-390):

- role_assignment: user (or, before their first sign-in, a lower-case pending email),
  jurisdiction, role, granted by and at, and ``tester`` (DIAAT-345: in the jurisdiction's
  test group, carried over from jurisdiction_membership.tester). Checks: the role is one of the four stored
  roles (SystemAdministrator is Entra only and Normal is gone, so neither can be
  stored); a jurisdiction is required for Judge, LegalTextManager and
  JurisdictionAdministrator and NULL for JurisdictionSuperAdministrator; a user or a
  pending email is present. Unique per (user, jurisdiction, role) and per
  (pending email, jurisdiction, role). A NULL jurisdiction is never equal to another in
  a unique index, so each uniqueness is a pair of partial indexes: one for a
  jurisdiction role and one for the global role.
- access_request and access_request_role (DIAAT-396): a request for Judge and/or
  LegalTextManager in a jurisdiction, pending, approved, rejected or withdrawn.
- system_feature_flag (DIAAT-392): a system-wide switch a SystemAdministrator sets.
- user.is_active (default true: DIAAT-382, a deactivated user is not re-created or
  re-enabled by signing in) and user.name (the sign-in's display name).
- audit_log.active_jurisdiction_id and active_role: the context an action was done in.

Backfill from jurisdiction_membership: ``admin`` becomes JurisdictionAdministrator and
``judge`` becomes Judge; a membership still pending by email stays pending (claimed at
first sign-in). Users of courtstranscribe are not touched. jurisdiction.auto_enrol is NOT
changed here: the previous release still reads it, and a migration that ships on ``master``
before the new code would change what the old pods do (first-time users would get no
jurisdiction and the old web has no request form). The new code never reads it (nobody is
enrolled automatically: a new user has no role until an administrator adds them or approves
their request), and 0010 sets it false.

The backfill is one-shot: memberships the previous release writes after this runs reach no
assignment. 0010 (DIAAT-402) repeats it, once the previous release is gone, for the memberships
that have no assignment.

Additive, so the previous release keeps working against this schema while a release
rolls out: jurisdiction_membership, user.role and jurisdiction.auto_enrol stay (dropped
in a later release), every new column is nullable or has a default, and the one
relaxation, ``user.role`` becoming nullable, lets the new code insert users with no
role while the previous release (which always names one) is unaffected. Pods still on
the previous release go on reading jurisdiction_membership, which this migration does
not change; the two stay consistent only until the new code first changes an
assignment, so a rollout back is clean up to then.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

STORED_ROLES = "'Judge', 'LegalTextManager', 'JurisdictionAdministrator', 'JurisdictionSuperAdministrator'"


def upgrade() -> None:
    op.create_table(
        "role_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("pending_email", sa.String(), nullable=True),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tester", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.ForeignKeyConstraint(["granted_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(f"role IN ({STORED_ROLES})", name="ck_role_assignment_role"),
        sa.CheckConstraint(
            "(role = 'JurisdictionSuperAdministrator' AND jurisdiction_id IS NULL)"
            " OR (role <> 'JurisdictionSuperAdministrator' AND jurisdiction_id IS NOT NULL)",
            name="ck_role_assignment_jurisdiction",
        ),
        sa.CheckConstraint("user_id IS NOT NULL OR pending_email IS NOT NULL", name="ck_role_assignment_who"),
        sa.CheckConstraint(
            "pending_email IS NULL OR pending_email = lower(pending_email)", name="ck_role_assignment_email_lower"
        ),
    )
    op.create_index("ix_role_assignment_user_id", "role_assignment", ["user_id"])
    op.create_index("ix_role_assignment_pending_email", "role_assignment", ["pending_email"])
    op.create_index("ix_role_assignment_jurisdiction_id", "role_assignment", ["jurisdiction_id"])
    op.create_index(
        "uq_role_assignment_user_jurisdiction_role",
        "role_assignment",
        ["user_id", "jurisdiction_id", "role"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND jurisdiction_id IS NOT NULL"),
    )
    op.create_index(
        "uq_role_assignment_user_global_role",
        "role_assignment",
        ["user_id", "role"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND jurisdiction_id IS NULL"),
    )
    op.create_index(
        "uq_role_assignment_pending_jurisdiction_role",
        "role_assignment",
        ["pending_email", "jurisdiction_id", "role"],
        unique=True,
        postgresql_where=sa.text("pending_email IS NOT NULL AND jurisdiction_id IS NOT NULL"),
    )
    op.create_index(
        "uq_role_assignment_pending_global_role",
        "role_assignment",
        ["pending_email", "role"],
        unique=True,
        postgresql_where=sa.text("pending_email IS NOT NULL AND jurisdiction_id IS NULL"),
    )

    op.create_table(
        "access_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')", name="ck_access_request_status"
        ),
    )
    op.create_index("ix_access_request_user_id", "access_request", ["user_id"])
    op.create_index("ix_access_request_jurisdiction_status", "access_request", ["jurisdiction_id", "status"])

    op.create_table(
        "access_request_role",
        sa.Column("access_request_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["access_request_id"], ["access_request.id"]),
        sa.PrimaryKeyConstraint("access_request_id", "role"),
        sa.CheckConstraint("role IN ('Judge', 'LegalTextManager')", name="ck_access_request_role_role"),
    )

    op.create_table(
        "system_feature_flag",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("name"),
    )

    # A default, so the previous release's inserts (which name neither) keep working.
    op.add_column("user", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("user", sa.Column("name", sa.String(), nullable=True))
    # A new user has no role: nothing to put here any more.
    op.alter_column("user", "role", existing_type=sa.String(), nullable=True)

    op.add_column("audit_log", sa.Column("active_jurisdiction_id", sa.Uuid(), nullable=True))
    op.add_column("audit_log", sa.Column("active_role", sa.String(length=32), nullable=True))

    # Memberships become assignments. ON CONFLICT DO NOTHING: two pending rows whose
    # emails differ only in case collapse to one.
    op.execute(
        sa.text(
            "INSERT INTO role_assignment"
            " (id, user_id, pending_email, jurisdiction_id, role, granted_by, granted_at, tester)"
            " SELECT gen_random_uuid(), m.user_id,"
            " CASE WHEN m.user_id IS NULL THEN lower(m.email) END,"
            " m.jurisdiction_id,"
            " CASE m.role WHEN 'admin' THEN 'JurisdictionAdministrator' ELSE 'Judge' END,"
            " NULL, m.created_datetime, m.tester"
            " FROM jurisdiction_membership m"
            " ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    # Assignments made since are lost; memberships (never changed by 0009) are what the
    # previous release reads. A user created since has no role: they get the lowest.
    op.drop_column("audit_log", "active_role")
    op.drop_column("audit_log", "active_jurisdiction_id")

    op.execute(sa.text("UPDATE \"user\" SET role = 'Normal' WHERE role IS NULL"))
    op.alter_column("user", "role", existing_type=sa.String(), nullable=False)
    op.drop_column("user", "name")
    op.drop_column("user", "is_active")

    op.drop_table("system_feature_flag")
    op.drop_table("access_request_role")
    op.drop_index("ix_access_request_jurisdiction_status", table_name="access_request")
    op.drop_index("ix_access_request_user_id", table_name="access_request")
    op.drop_table("access_request")

    for index in (
        "uq_role_assignment_pending_global_role",
        "uq_role_assignment_pending_jurisdiction_role",
        "uq_role_assignment_user_global_role",
        "uq_role_assignment_user_jurisdiction_role",
        "ix_role_assignment_jurisdiction_id",
        "ix_role_assignment_pending_email",
        "ix_role_assignment_user_id",
    ):
        op.drop_index(index, table_name="role_assignment")
    op.drop_table("role_assignment")
    # auto_enrol was not touched by the upgrade, so there is nothing to restore (0010 sets it).

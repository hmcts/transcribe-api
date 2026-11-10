"""Jurisdiction stages, display names, services and testers (DIAAT-345).

A new jurisdiction is set up as a draft, tried by a test group, then switched on for
everyone, all without a release (domain/jurisdictions.py):

- jurisdiction.stage: 'draft' (only its admins see it), 'test' (its testers and
  admins) or 'live' (every member). Its own column, beside ``enabled``, which still
  switches a jurisdiction off for everyone whatever its stage;
- jurisdiction.display_name: what users see, e.g. "Employment Tribunals (ET)"
  (NULL: its name);
- jurisdiction.service: the service it is for, where that applies (NULL: none);
- jurisdiction_membership.tester: a member in the test group.

Schema only: no row of jurisdiction is written, so an admin's live edit is kept
(tests/unit/test_migrations.py). Every existing jurisdiction is live, as it was
before stages, by the column's default; so is a row the previous release inserts
while this one rolls out. The application creates new jurisdictions as drafts.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

STAGES = ("draft", "test", "live")


def upgrade() -> None:
    op.add_column(
        "jurisdiction", sa.Column("stage", sa.String(length=8), nullable=False, server_default=sa.text("'live'"))
    )
    op.create_check_constraint(
        "ck_jurisdiction_stage", "jurisdiction", f"stage IN ({', '.join(repr(s) for s in STAGES)})"
    )
    op.add_column("jurisdiction", sa.Column("display_name", sa.String(), nullable=True))
    op.add_column("jurisdiction", sa.Column("service", sa.String(), nullable=True))
    op.add_column(
        "jurisdiction_membership",
        sa.Column("tester", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    # Without its stage, a draft or test jurisdiction would be seen by every member:
    # it is switched off instead.
    op.execute(sa.text("UPDATE jurisdiction SET enabled = false WHERE stage <> 'live'"))
    op.drop_column("jurisdiction_membership", "tester")
    op.drop_column("jurisdiction", "service")
    op.drop_column("jurisdiction", "display_name")
    op.drop_constraint("ck_jurisdiction_stage", "jurisdiction", type_="check")
    op.drop_column("jurisdiction", "stage")

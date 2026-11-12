"""Templates tried by a test group first; the service lead's sign-off (DIAAT-347).

A template, like a jurisdiction (0007), goes from draft to its test group to every
judge, and both are switched on for everyone only with the service lead's sign-off
recorded (jurisdictions_admin.set_stage):

- jurisdiction_template.stage: 'draft' (no one chooses it), 'test' (the
  jurisdiction's testers and admins) or 'live' (all its judges). A template judges
  could choose (enabled) is live, as it was; any other a draft. ``enabled`` stays,
  kept equal to live, for the previous release's pods while this one rolls out.
  Those pods write ``enabled`` alone: ck_jurisdiction_template_enabled_is_live refuses
  one switching on a template that is not live, which would reach every judge without
  the test group or the sign-off. Switching one off there is allowed; the code takes
  it as a draft (jurisdiction_templates.stage_of);
- jurisdiction.signed_off_* and jurisdiction_template.signed_off_*: the service
  lead's name, when, and the admin who recorded it. NULL for what was live before.

No row of jurisdiction or document_content is written, so live edits are kept
(tests/unit/test_migrations.py).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

STAGES = ("draft", "test", "live")
SIGNED_OFF = ("jurisdiction", "jurisdiction_template")


def upgrade() -> None:
    op.add_column(
        "jurisdiction_template",
        sa.Column("stage", sa.String(length=8), nullable=False, server_default=sa.text("'draft'")),
    )
    op.create_check_constraint(
        "ck_jurisdiction_template_stage", "jurisdiction_template", f"stage IN ({', '.join(repr(s) for s in STAGES)})"
    )
    op.execute(sa.text("UPDATE jurisdiction_template SET stage = 'live' WHERE enabled"))
    op.create_check_constraint(
        "ck_jurisdiction_template_enabled_is_live", "jurisdiction_template", "NOT enabled OR stage = 'live'"
    )
    for table in SIGNED_OFF:
        op.add_column(table, sa.Column("signed_off_by", sa.String(length=200), nullable=True))
        op.add_column(table, sa.Column("signed_off_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("signed_off_recorded_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    # Without its stage, a template in test would be chosen by no one or everyone: only
    # a live one stays available (enabled is kept equal to live).
    for table in reversed(SIGNED_OFF):
        op.drop_column(table, "signed_off_recorded_by")
        op.drop_column(table, "signed_off_at")
        op.drop_column(table, "signed_off_by")
    op.drop_constraint("ck_jurisdiction_template_enabled_is_live", "jurisdiction_template", type_="check")
    op.drop_constraint("ck_jurisdiction_template_stage", "jurisdiction_template", type_="check")
    op.drop_column("jurisdiction_template", "stage")

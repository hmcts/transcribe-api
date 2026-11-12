"""Templates tried by a test group first; the service lead's sign-off (DIAAT-347).

A template, like a jurisdiction (0007), goes from draft to its test group to every
judge, and both are switched on for everyone only with the service lead's sign-off
recorded (jurisdictions_admin.set_stage):

- jurisdiction_template.stage: 'draft' (no one chooses it), 'test' (the
  jurisdiction's testers and admins) or 'live' (all its judges). A template judges
  could choose (enabled) is live, as it was; any other a draft. ``enabled`` stays,
  kept equal to live, for the previous release's pods while this one rolls out;
- jurisdiction.signed_off_* and jurisdiction_template.signed_off_*: the service
  lead's name, when, and the admin who recorded it. NULL for what was live before.

The previous release's pods know neither the sign-off nor a template's stage, so the
rules are kept by the database, whichever release writes (*_sign_off_guard triggers):

- going live needs a sign-off written with it: a row that is not live has none (it is
  cleared on leaving live), so the previous release cannot put one live;
- a template switched off by ``enabled`` alone leaves live: it becomes a draft, its
  sign-off cleared, and ck_jurisdiction_template_enabled_is_live refuses switching on
  one that is not live. Switching it on again needs the new release and a sign-off.

What is live already stays live, and keeps being saved.

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

# Before every update of either table. Only a template's ``enabled`` moves its stage:
# a jurisdiction's is a separate off switch (DIAAT-345).
GUARD = """
CREATE FUNCTION {table}_sign_off_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  {switched_off}IF NEW.stage <> 'live' THEN
    NEW.signed_off_by := NULL;
    NEW.signed_off_at := NULL;
    NEW.signed_off_recorded_by := NULL;
  ELSIF OLD.stage <> 'live' AND NEW.signed_off_by IS NULL THEN
    RAISE EXCEPTION 'The service lead''s sign-off is needed to switch % % on for everyone', TG_TABLE_NAME, NEW.id
      USING ERRCODE = 'check_violation', CONSTRAINT = '{table}_sign_off_guard';
  END IF;
  RETURN NEW;
END
$$
"""
TEMPLATE_SWITCHED_OFF = """IF NOT NEW.enabled AND NEW.stage = 'live' THEN
    NEW.stage := 'draft';
  END IF;
  """


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
        switched_off = TEMPLATE_SWITCHED_OFF if table == "jurisdiction_template" else ""
        op.execute(sa.text(GUARD.format(table=table, switched_off=switched_off)))
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_sign_off_guard BEFORE UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {table}_sign_off_guard()"
            )
        )


def downgrade() -> None:
    # Without its stage, a template in test would be chosen by no one or everyone: only
    # a live one stays available (enabled is kept equal to live).
    for table in reversed(SIGNED_OFF):
        op.execute(sa.text(f"DROP TRIGGER {table}_sign_off_guard ON {table}"))
        op.execute(sa.text(f"DROP FUNCTION {table}_sign_off_guard()"))
        op.drop_column(table, "signed_off_recorded_by")
        op.drop_column(table, "signed_off_at")
        op.drop_column(table, "signed_off_by")
    op.drop_constraint("ck_jurisdiction_template_enabled_is_live", "jurisdiction_template", type_="check")
    op.drop_constraint("ck_jurisdiction_template_stage", "jurisdiction_template", type_="check")
    op.drop_column("jurisdiction_template", "stage")

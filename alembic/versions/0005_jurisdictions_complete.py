"""Jurisdictions, completed: one draft per user per jurisdiction (DIAAT-322).

The second half of 0004. Every pod migrates to head as it starts, so both run in the
same release, before its rollout: keeping the old one-per-user key any longer would
refuse a judge's draft in their second jurisdiction (a 500 on every autosave there).
While the rollout lasts (a few minutes), the old pods' draft saves fail, as their
upsert names that key; the page keeps the text and saves again. Their other writes
land in the IAC (0004's column defaults).

- any record without a jurisdiction is put in the IAC, as 0004 put the earlier ones;
- any user with no membership is made an IAC judge, as 0004 made the earlier ones
  (run with 0004, before anyone can be removed from a jurisdiction);
- live_transcript_draft.jurisdiction_id becomes NOT NULL, and the one-draft-per-user
  key goes: a judge in two jurisdictions has a draft in each.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

IAC_ID = "6a1c0f3e-3b7d-4c2a-9e51-1ac000000001"  # created by 0004


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO jurisdiction_membership (id, jurisdiction_id, user_id, role, created_datetime)"
            " SELECT gen_random_uuid(), :iac, u.id, 'judge', now() FROM \"user\" u"
            " WHERE NOT EXISTS (SELECT 1 FROM jurisdiction_membership m WHERE m.user_id = u.id)"
        ).bindparams(iac=IAC_ID)
    )
    op.execute(
        sa.text("UPDATE transcription SET jurisdiction_id = :iac WHERE jurisdiction_id IS NULL").bindparams(iac=IAC_ID)
    )
    op.execute(
        sa.text(
            "UPDATE transcription_job SET jurisdiction_id = :iac WHERE jurisdiction_id IS NULL"
        ).bindparams(iac=IAC_ID)
    )
    op.execute(
        sa.text("UPDATE live_transcript_draft SET jurisdiction_id = :iac WHERE jurisdiction_id IS NULL").bindparams(
            iac=IAC_ID
        )
    )
    op.alter_column("live_transcript_draft", "jurisdiction_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("live_transcript_draft_user_id_key", "live_transcript_draft", type_="unique")


def downgrade() -> None:
    # A user has one draft again: only their IAC draft is kept.
    op.execute(sa.text("DELETE FROM live_transcript_draft WHERE jurisdiction_id <> :iac").bindparams(iac=IAC_ID))
    op.create_unique_constraint("live_transcript_draft_user_id_key", "live_transcript_draft", ["user_id"])
    op.alter_column("live_transcript_draft", "jurisdiction_id", existing_type=sa.Uuid(), nullable=True)

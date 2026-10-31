"""Jurisdictions, completed: one draft per user per jurisdiction (DIAAT-322).

The second half of 0004, for once no pod runs the release before DIAAT-322 (deploy it
in a later release than 0004, or accept that a rollout of both together can fail the
old pods' draft saves for its few minutes):

- records the old pods wrote during 0004's rollout, without a jurisdiction, are put
  in the IAC, as 0004 put the earlier ones (recording jobs of API-key callers, which
  have no user, stay NULL);
- users who first signed in through an old pod then, and so were never enrolled,
  are made IAC judges, as 0004 made the earlier ones;
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
            "UPDATE transcription_job SET jurisdiction_id = :iac WHERE jurisdiction_id IS NULL AND user_id IS NOT NULL"
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

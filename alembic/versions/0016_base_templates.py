"""A template shared as a base for other jurisdictions (DIAAT-336).

- jurisdiction_template.shared_as_base: its jurisdiction's admin shares it, so admins of
  any jurisdiction can start a template of their own from a copy of it. False for every
  template there is.
- jurisdiction_template.started_from: for a template started that way, the template it
  was copied from as it was then (jurisdiction_templates.add_from_base). NULL for one
  uploaded in its own jurisdiction. A copy is the jurisdiction's own: later changes to
  the shared one do not change it.

Columns with defaults only: no row of jurisdiction or document_content is written, so
live edits are kept (tests/unit/test_migrations.py). The previous release's pods do not
know the columns and keep working while this one rolls out.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jurisdiction_template",
        sa.Column("shared_as_base", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "jurisdiction_template",
        sa.Column("started_from", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jurisdiction_template", "started_from")
    op.drop_column("jurisdiction_template", "shared_as_base")

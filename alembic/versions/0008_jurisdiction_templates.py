"""Word templates uploaded for a jurisdiction (DIAAT-344).

jurisdiction_template: a jurisdiction's uploaded Word template, the fill-in boxes
found in it and the field linked to each box (documents/uploaded_templates.py).
Judges of the jurisdiction choose one on the recording page once it is enabled.

A new table only: no row of jurisdiction or document_content is written, so live
edits are kept (tests/unit/test_migrations.py). The previous release's pods do not
know the table and keep working while this one rolls out.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jurisdiction_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("service", sa.String(length=200), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("boxes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jurisdiction_id", "name", name="uq_jurisdiction_template_name"),
    )
    op.create_index(
        op.f("ix_jurisdiction_template_jurisdiction_id"), "jurisdiction_template", ["jurisdiction_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_jurisdiction_template_jurisdiction_id"), table_name="jurisdiction_template")
    op.drop_table("jurisdiction_template")

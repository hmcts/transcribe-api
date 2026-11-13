"""Every saved version of a jurisdiction's settings and of a template (DIAAT-348).

setting_version: one row per save, numbered from 1 for each jurisdiction or template,
with who saved it and when, and a copy of what was saved (setting_history.py). The
history compares each version with the one before; going back saves an earlier one's
content again, as a new version.

template_file: the template files those versions name, once each, by SHA-256: saving
a template's fields again does not copy its file.

New tables only: no row of jurisdiction or document_content is written, so live edits
are kept (tests/unit/test_migrations.py). The history of each jurisdiction and template
starts with its first save after this release; what was there before is recorded then,
as the version before it. The previous release's pods do not know the tables and keep
working while this one rolls out.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_file",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("sha256"),
    )
    op.create_table(
        "setting_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("saved_by", sa.Uuid(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_from", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "object_id", "number", name="uq_setting_version_number"),
        sa.CheckConstraint("kind IN ('jurisdiction', 'template')", name="ck_setting_version_kind"),
    )
    op.create_index(op.f("ix_setting_version_jurisdiction_id"), "setting_version", ["jurisdiction_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_setting_version_jurisdiction_id"), table_name="setting_version")
    op.drop_table("setting_version")
    op.drop_table("template_file")

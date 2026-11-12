"""Drop the access data 0010 retired (DIAAT-405).

0010 renamed ``jurisdiction_membership`` to ``jurisdiction_membership_legacy`` and turned
``jurisdiction.auto_enrol`` off, keeping both while the 0009 release could still serve. Every environment
now runs a release newer than 0010, so both go:

- ``jurisdiction_membership_legacy``, with its keys, indexes and constraints (its id is a UUID: no sequence).
  Nothing references it. Its rows are not exported or checked first: access lost with them is given again
  by hand.
- ``jurisdiction.auto_enrol``.

Both are dropped only if they are still there, so a database where they are already gone migrates too.

Locks: dropping the column takes ACCESS EXCLUSIVE on ``jurisdiction``, and dropping the table's foreign
keys takes it on ``user`` as well; live requests read both all the time. As in 0010, every lock is taken up
front, each attempt giving up after LOCK_TIMEOUT (shorter than PostgreSQL's deadlock_timeout) and releasing
what it took, so a request in progress is never the one aborted. Nothing reads the legacy table, so the DROP
gets its lock without waiting.

Downgrade puts back an empty table and the column (false, with 0010's default), in the shape 0010 left
them, so 0010's and the older downgrades still run. The dropped rows do not come back.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12
"""

import logging
import time

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

LEGACY_TABLE = "jurisdiction_membership_legacy"

LOCKS = (
    "LOCK TABLE jurisdiction IN ACCESS EXCLUSIVE MODE",
    'LOCK TABLE "user" IN ACCESS EXCLUSIVE MODE',
)
LOCK_TIMEOUT = "500ms"
LOCK_ATTEMPTS = 120
LOCK_NOT_AVAILABLE = "55P03"


def _lock(bind) -> None:
    """Take LOCKS, all or none per attempt (a savepoint), until they are free. lock_timeout stays set
    after: a wait that was not foreseen fails the migration (the pod restarts and tries again)."""
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        savepoint = bind.begin_nested()
        try:
            for statement in LOCKS:
                bind.execute(sa.text(statement))
        except sa.exc.OperationalError as exc:
            savepoint.rollback()
            if getattr(exc.orig, "pgcode", None) != LOCK_NOT_AVAILABLE or attempt == LOCK_ATTEMPTS:
                raise
            logger.info("DIAAT-405: tables busy, trying again (attempt %d)", attempt)
            time.sleep(0.25)
        else:
            savepoint.commit()
            return


def upgrade() -> None:
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    if context.is_offline_mode():
        for statement in LOCKS:
            op.execute(sa.text(statement))
    else:
        _lock(op.get_bind())
    op.execute(sa.text(f"DROP TABLE IF EXISTS {LEGACY_TABLE}"))
    op.execute(sa.text("ALTER TABLE jurisdiction DROP COLUMN IF EXISTS auto_enrol"))


def downgrade() -> None:
    op.add_column("jurisdiction", sa.Column("auto_enrol", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Named as 0004 and 0007 named them: the table was renamed, its keys and indexes kept their names.
    op.create_table(
        LEGACY_TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tester", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(
            ["jurisdiction_id"], ["jurisdiction.id"], name="jurisdiction_membership_jurisdiction_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="jurisdiction_membership_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="jurisdiction_membership_pkey"),
        sa.UniqueConstraint("jurisdiction_id", "user_id", name="uq_jurisdiction_membership_user"),
        sa.CheckConstraint("role IN ('judge', 'admin')", name="ck_jurisdiction_membership_role"),
        sa.CheckConstraint("user_id IS NOT NULL OR email IS NOT NULL", name="ck_jurisdiction_membership_who"),
    )
    op.create_index("ix_jurisdiction_membership_jurisdiction_id", LEGACY_TABLE, ["jurisdiction_id"])
    op.create_index("ix_jurisdiction_membership_user_id", LEGACY_TABLE, ["user_id"])
    op.create_index(
        "uq_jurisdiction_membership_pending_email",
        LEGACY_TABLE,
        ["jurisdiction_id", "email"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )

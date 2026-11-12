"""Apply database migrations when the API starts.

There is one Alembic history (alembic/), whose first revision is the baseline of
the merged schema. Deployed environments set RUN_MIGRATIONS=true and every API
pod runs `upgrade head` on startup. Pods start together, so the upgrade runs
under a Postgres advisory lock: the first pod migrates, the others wait and then
find nothing to do.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import MetaData, text

from alembic import command

# Arbitrary, fixed key for pg_advisory_lock; only this code uses it.
MIGRATION_LOCK_KEY = 7243184601


def _alembic_ini() -> Path:
    """alembic.ini sits at the repo root locally and at /opt/app in the image."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("alembic.ini not found above " + str(Path(__file__).parent))


def alembic_config() -> Config:
    ini = _alembic_ini()
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "alembic"))
    return config


def target_metadata() -> MetaData:
    """The merged models' metadata, with every table registered."""
    from sqlmodel import SQLModel

    import transcribe_api.domain.models_dictation
    import transcribe_api.domain.models_recording  # noqa: F401

    return SQLModel.metadata


# In the database but no longer in the models (0010): kept for recovery, or until the release whose
# pods still select it is gone. Autogenerate and the drift test leave them alone.
RETIRED_TABLES = frozenset({"jurisdiction_membership_legacy"})
RETIRED_COLUMNS = frozenset({("jurisdiction", "auto_enrol")})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name not in RETIRED_TABLES
    if type_ == "column":
        return (obj.table.name, name) not in RETIRED_COLUMNS
    return True


def run_migrations(engine=None) -> None:
    """Upgrade the database to the latest revision, one process at a time."""
    if engine is None:
        from transcribe_api.runtime.db_connection import get_engine

        engine = get_engine()

    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        connection.commit()
        try:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()

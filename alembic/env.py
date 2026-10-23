"""Alembic environment for the merged transcribe-api schema.

One history for the whole database. At API startup, runtime/migrations.py runs
`upgrade head` on its own connection (passed in config.attributes, holding the
migration lock). From the command line, the connection string comes from
DATABASE_CONNECTION_STRING.
"""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from transcribe_api.runtime.migrations import target_metadata

config = context.config

# Only configure logging for command-line runs; at startup the app owns logging.
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def get_url() -> str:
    return os.getenv("DATABASE_CONNECTION_STRING", "postgresql://localhost:5432/transcription_svc")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata())
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

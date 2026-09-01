from __future__ import annotations

from functools import lru_cache

from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.runtime.settings_recording import get_settings


@lru_cache
def get_engine():
    settings = get_settings()
    return create_engine(
        settings.DATABASE_CONNECTION_STRING,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session():
    with Session(get_engine()) as session:
        yield session

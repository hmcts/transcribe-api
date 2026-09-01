"""
Database connection utilities for both sync and async operations.
"""

import re
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import create_engine as create_sync_engine

from transcribe_api.runtime.settings_dictation import get_settings


def _requires_ssl(database_url: str) -> bool:
    """Return True if SSL is required (sslmode present or Azure Flexible Server host)."""
    parsed = urlparse(database_url)
    q = parse_qs(parsed.query or "")
    if "sslmode" in q:
        # require/verify-* imply SSL; disable means no SSL
        mode = (q["sslmode"][-1] or "").lower()
        return mode != "disable"
    # Azure Flexible Server always expects TLS
    return parsed.hostname and parsed.hostname.endswith("postgres.database.azure.com")


def _to_asyncpg_url(database_url: str, require_ssl: bool) -> str:
    """
    Convert postgresql:// URL to postgresql+asyncpg://.
    If require_ssl is True, normalize any sslmode=... to ssl=true (asyncpg-style).
    Otherwise, strip sslmode (if present) and avoid adding ssl=true.
    """
    url = re.sub(r"^postgresql://", "postgresql+asyncpg://", database_url, count=1)

    # Remove any existing asyncpg-incompatible sslmode param
    url = re.sub(r"([?&])sslmode=[^&]+(&)?", lambda m: m.group(1) if m.group(2) else "", url)

    if require_ssl:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}ssl=true"

    # Clean up any trailing ? or & (edge cases)
    url = re.sub(r"[?&]$", "", url)
    return url


# ---------- Sync engine (psycopg2 via SQLModel) ----------
_sync_engine_singleton = None


def get_engine():
    """Return the process-wide synchronous database engine, creating it once."""
    global _sync_engine_singleton  # noqa: PLW0603
    if _sync_engine_singleton is not None:
        return _sync_engine_singleton
    database_url = get_settings().DATABASE_CONNECTION_STRING
    _sync_engine_singleton = create_sync_engine(database_url, echo=False, pool_pre_ping=True)
    return _sync_engine_singleton


# ---------- Async engine (asyncpg) ----------
_async_engine_singleton = None
_AsyncSessionLocal = None


def get_async_engine():
    global _async_engine_singleton, _AsyncSessionLocal  # noqa: PLW0603 - Singleton pattern requires global state
    if _async_engine_singleton is not None:
        return _async_engine_singleton

    settings = get_settings()
    db_url = settings.DATABASE_CONNECTION_STRING
    need_ssl = _requires_ssl(db_url)

    asyncpg_url = _to_asyncpg_url(db_url, require_ssl=need_ssl)

    connect_args = {}
    if need_ssl:
        # Verify server cert using system CAs (ensure ca-certificates installed in image)
        connect_args["ssl"] = ssl.create_default_context()

    _async_engine_singleton = create_async_engine(
        asyncpg_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=900,
    )
    _AsyncSessionLocal = async_sessionmaker(_async_engine_singleton, expire_on_commit=False, class_=AsyncSession)
    return _async_engine_singleton


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async SQLAlchemy session with commit/rollback semantics.
    """
    # Ensure engine/sessionmaker are initialized lazily
    get_async_engine()
    async with _AsyncSessionLocal() as session:  # type: ignore[arg-type]
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

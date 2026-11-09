"""System feature flags (DIAAT-388, DIAAT-392): the layer that says whether a switch is on.

A flag is a row of ``system_feature_flag`` (name, value) that a SystemAdministrator sets; a flag
with no row has its default, which comes from configuration. Today there is one flag:

``dictation_enabled``
    Whether the dictation routes work (DIAAT-388). The default is the ``DICTATION_ENABLED``
    setting (true; the chart turns it off in prod). The ``dictation.use`` permission check
    (domain/auth/authorisation.py) refuses every dictation route with 403 ``dictation_disabled``
    when it is off, and audits the refusal. Hearing-recording routes do not look at it.

A flag is read with a short in-process cache (``CACHE_TTL_SECONDS``, 5 s): the permission check
reads ``dictation_enabled`` on every dictation request, and that is one database read per five
seconds per process instead of one per request. A change made through this process
(``set_flag``, the admin PUT) clears the cache at once, so it takes effect immediately for the
process that took it; another replica sees it within the TTL. If the database cannot be read
the default applies, and that answer is not cached: a database outage must not switch a
feature on or off, and the next request reads again.

The admin endpoints that change a flag are DIAAT-392 (api/routes_admin_flags.py).
``GET /api/flags`` (api/routes_flags.py) tells the web app what is on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import SystemFeatureFlag, User
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.runtime.settings_dictation import get_settings

logger = logging.getLogger(__name__)

DICTATION_ENABLED = "dictation_enabled"

# Every flag, with the setting its default comes from.
FLAG_DEFAULTS: dict[str, str] = {DICTATION_ENABLED: "DICTATION_ENABLED"}
# What each flag does, for the admin list.
FLAG_DESCRIPTIONS: dict[str, str] = {
    DICTATION_ENABLED: "Whether dictation (recording and documents from dictation) is switched on for every user.",
}

CACHE_TTL_SECONDS = 5.0

# name -> (value, expires at, on the monotonic clock). Only values read from the database or
# defaulted because there is no row: never the fallback of an unreadable database.
_cache: dict[str, tuple[bool, float]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    """Forget what was read, so the next read goes to the database (a flag was changed here)."""
    with _cache_lock:
        _cache.clear()


class UnknownFlagError(KeyError):
    """No such flag."""


def _stored_value(name: str) -> tuple[bool | None, bool]:
    """(the flag's row value or None if there is none, whether the database could be read)."""
    try:
        with Session(get_engine()) as session:
            row = session.exec(select(SystemFeatureFlag).where(SystemFeatureFlag.name == name)).first()
    except Exception as exc:
        logger.warning("Could not read feature flag %s (%s): using its default", name, type(exc).__name__)
        return None, False
    return (None if row is None else bool(row.value)), True


def default_value(name: str) -> bool:
    """The flag's default, from configuration."""
    return bool(getattr(get_settings(), FLAG_DEFAULTS[name]))


def flag_value(name: str) -> bool:
    """Whether the flag is on: its row if it has one, else its default; cached for
    ``CACHE_TTL_SECONDS``. Synchronous (a database read on a miss): call it from a threadpool on
    the event loop."""
    if name not in FLAG_DEFAULTS:
        msg = f"unknown feature flag: {name}"
        raise UnknownFlagError(msg)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]
    stored, readable = _stored_value(name)
    value = default_value(name) if stored is None else stored
    if readable:
        with _cache_lock:
            _cache[name] = (value, now + CACHE_TTL_SECONDS)
    return value


def is_dictation_enabled() -> bool:
    """Whether the dictation routes are on (DIAAT-388)."""
    return flag_value(DICTATION_ENABLED)


@dataclass(frozen=True)
class FlagState:
    """A flag as the admin list shows it."""

    name: str
    value: bool
    description: str
    updated_at: datetime | None
    # The email of the user who last set it; None while it has its default.
    updated_by: str | None


def list_flags(session: Session) -> list[FlagState]:
    """Every flag with its effective value (the row, else the default) and who last set it.
    Read from the database, not the cache: what an administrator looks at is what is stored."""
    rows = {row.name: row for row in session.exec(select(SystemFeatureFlag)).all()}
    emails = {
        user.id: user.email
        for user in session.exec(
            select(User).where(User.id.in_([row.updated_by for row in rows.values() if row.updated_by]))  # type: ignore[attr-defined]
        ).all()
    }
    states = []
    for name in FLAG_DEFAULTS:
        row = rows.get(name)
        states.append(
            FlagState(
                name=name,
                value=default_value(name) if row is None else bool(row.value),
                description=FLAG_DESCRIPTIONS[name],
                updated_at=None if row is None else row.updated_at,
                updated_by=None if row is None or row.updated_by is None else emails.get(row.updated_by),
            )
        )
    return states


def current_value(session: Session, name: str) -> bool:
    """The flag's effective value, read from the database (not the cache), for the audit's 'from'."""
    if name not in FLAG_DEFAULTS:
        raise UnknownFlagError(name)
    row = session.exec(select(SystemFeatureFlag).where(SystemFeatureFlag.name == name)).first()
    return default_value(name) if row is None else bool(row.value)


def set_flag(session: Session, name: str, value: bool, *, updated_by: UUID | None) -> bool:
    """Store the flag's value (an upsert) and clear the cache. Returns the value it had.
    Commits."""
    if name not in FLAG_DEFAULTS:
        raise UnknownFlagError(name)
    before = current_value(session, name)
    row = session.exec(select(SystemFeatureFlag).where(SystemFeatureFlag.name == name)).first()
    if row is None:
        row = SystemFeatureFlag(name=name, value=value)
    row.value = value
    row.updated_at = datetime.now(UTC)
    row.updated_by = updated_by
    session.add(row)
    session.commit()
    invalidate_cache()
    return before

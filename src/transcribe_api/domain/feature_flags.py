"""System feature flags (DIAAT-388, DIAAT-392): the layer that says whether a switch is on.

A flag is a row of ``system_feature_flag`` (name, value) that a SystemAdministrator sets; a flag
with no row has its default, which comes from configuration. Today there is one flag:

``dictation_enabled``
    Whether the dictation routes work (DIAAT-388). The default is the ``DICTATION_ENABLED``
    setting (true; the chart turns it off in prod). The ``dictation.use`` permission check
    (domain/auth/authorisation.py) refuses every dictation route with 403 ``dictation_disabled``
    when it is off, and audits the refusal. Hearing-recording routes do not look at it.

A flag is read at the time of the request, not cached for the life of the process, so changing
the row (or, with no row, the setting and a restart) takes effect at once. If the database
cannot be read the default applies: a database outage must not switch a feature on or off.

The admin endpoints that change a flag are DIAAT-392. ``GET /api/flags`` (api/routes_flags.py)
tells the web app what is on.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import SystemFeatureFlag
from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.runtime.settings_dictation import get_settings

logger = logging.getLogger(__name__)

DICTATION_ENABLED = "dictation_enabled"

# Every flag, with the setting its default comes from.
FLAG_DEFAULTS: dict[str, str] = {DICTATION_ENABLED: "DICTATION_ENABLED"}


def _stored_value(name: str) -> bool | None:
    """The flag's row, or None if there is none (or the database cannot be read)."""
    try:
        with Session(get_engine()) as session:
            row = session.exec(select(SystemFeatureFlag).where(SystemFeatureFlag.name == name)).first()
    except Exception as exc:
        logger.warning("Could not read feature flag %s (%s): using its default", name, type(exc).__name__)
        return None
    return None if row is None else bool(row.value)


def default_value(name: str) -> bool:
    """The flag's default, from configuration."""
    return bool(getattr(get_settings(), FLAG_DEFAULTS[name]))


def flag_value(name: str) -> bool:
    """Whether the flag is on: its row if it has one, else its default. Synchronous (a
    database read): call it from a threadpool on the event loop."""
    if name not in FLAG_DEFAULTS:
        msg = f"unknown feature flag: {name}"
        raise KeyError(msg)
    stored = _stored_value(name)
    return default_value(name) if stored is None else stored


def is_dictation_enabled() -> bool:
    """Whether the dictation routes are on (DIAAT-388)."""
    return flag_value(DICTATION_ENABLED)

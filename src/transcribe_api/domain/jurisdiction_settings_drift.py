"""Report where the live IAC settings differ from the settings it was created with.

An admin edits a jurisdiction's wording in the live service (jurisdiction.settings); a
deployment never writes it: migration 0004 only inserts the IAC row. This check tells
the product team, once at startup, where the live settings differ from that shipped
seed, so they can decide whether a change needs applying by hand. Only key names and
counts are reported, never the wording.

The live row is matched by code. Nothing here writes to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Startup waits on this check, so a lock or a slow query must not hold up readiness.
_DB_TIMEOUT = "5s"

SHIPPED_CODE = "IAC"

# A copy of IAC_SETTINGS in alembic/versions/0004_jurisdictions.py, which is frozen
# (a migration module is not imported by the app). tests/unit/test_jurisdiction_settings_drift.py
# fails if the two differ.
SHIPPED_IAC_SETTINGS: dict[str, Any] = {
    "wording": {
        "home.intro": (
            "Supporting judicial work through live transcription and enablement of judges to focus on "
            "proceedings and engage with the parties."
        ),
        "help.subtitle": "Getting started with Bench Notes for the First-tier Tribunal Immigration and Asylum Chamber",
        "nav.recordings": "Hearing recordings",
        "records.column_title": "Hearing",
        "records.column_type": "Hearing type",
        "records.search_placeholder": "Search hearings or dates",
        "records.search_hint": "Search recent recordings by hearing title or date.",
        "records.filter_label": "Filter recent recordings by hearing type",
        "records.filter_placeholder": "Filter by hearing type",
        "records.filter_all": "All hearing types",
        "records.no_match": "No hearings matched your current search or filters.",
        "records.open": "Open hearing",
        "records.rename_hint": "Update the title shown in the hearings list.",
        "records.no_transcript": "No saved transcript was found for this hearing.",
        "autosave.record_name": "case record",
        "alerts.speech_lost_guidance": (
            "Pause then restart recording to restart dictation. If this warning comes back, record this "
            "decision in another way and raise with your Support team for investigation."
        ),
    },
    "template": "iac_decision",
    "features": {"hearing_recordings": True},
}


@dataclass
class SettingsDrift:
    """Wording keys as in domain/jurisdictions.WORDING_KEYS, feature flags by name."""

    wording_edited: list[str] = field(default_factory=list)  # in both, live differs from the shipped text
    wording_live_only: list[str] = field(default_factory=list)  # set in the live service only
    wording_default_only: list[str] = field(
        default_factory=list
    )  # shipped, missing live (blank = the web app's default)
    template_changed: bool = False
    features_changed: list[str] = field(default_factory=list)  # flag differs, or is on one side only

    def __bool__(self) -> bool:
        return bool(
            self.wording_edited
            or self.wording_live_only
            or self.wording_default_only
            or self.template_changed
            or self.features_changed
        )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def compare_settings(live: dict[str, Any] | None, shipped: dict[str, Any]) -> SettingsDrift:
    live, shipped = _mapping(live), _mapping(shipped)
    live_wording, shipped_wording = _mapping(live.get("wording")), _mapping(shipped.get("wording"))
    live_features, shipped_features = _mapping(live.get("features")), _mapping(shipped.get("features"))
    return SettingsDrift(
        wording_edited=sorted(
            k for k in live_wording.keys() & shipped_wording.keys() if live_wording[k] != shipped_wording[k]
        ),
        wording_live_only=sorted(live_wording.keys() - shipped_wording.keys()),
        wording_default_only=sorted(shipped_wording.keys() - live_wording.keys()),
        template_changed=live.get("template") != shipped.get("template"),
        features_changed=sorted(
            k
            for k in live_features.keys() | shipped_features.keys()
            if k not in live_features or k not in shipped_features or live_features[k] != shipped_features[k]
        ),
    )


def check_settings_drift() -> SettingsDrift | None:
    """Warn and record settings.drift_detected when the live IAC settings differ from the shipped ones.

    Read-only, and never raises: a failure here must not stop startup. Returns the
    drift, or None when there is none or the check could not run.
    """
    try:
        from sqlalchemy import text
        from sqlmodel import Session, select

        from transcribe_api.domain.models_dictation import Jurisdiction
        from transcribe_api.runtime.db_connection import get_engine

        with Session(get_engine()) as session:
            # Bound the read on the database side: a thread blocked on an exclusive lock cannot be cancelled.
            session.execute(text(f"SET LOCAL lock_timeout = '{_DB_TIMEOUT}'"))
            session.execute(text(f"SET LOCAL statement_timeout = '{_DB_TIMEOUT}'"))
            row = session.exec(select(Jurisdiction).where(Jurisdiction.code == SHIPPED_CODE)).first()
            live = row.settings if row is not None else None
        if live is None:
            return None  # No IAC row: nothing shipped to compare with.

        drift = compare_settings(live, SHIPPED_IAC_SETTINGS)
        if not drift:
            return None

        from hmcts_otel import record_event

        from transcribe_api.runtime.telemetry import (
            ATTR_SETTINGS_DEFAULT_ONLY_COUNT,
            ATTR_SETTINGS_FEATURES_CHANGED_COUNT,
            ATTR_SETTINGS_LIVE_ONLY_COUNT,
            ATTR_SETTINGS_TEMPLATE_CHANGED,
            ATTR_SETTINGS_WORDING_EDITED_COUNT,
            EVENT_SETTINGS_DRIFT_DETECTED,
        )

        logger.warning(
            "Live %s settings differ from the shipped ones; the live settings are kept. "
            "Wording edited live (%d): %s. Wording only live (%d): %s. Wording only in the shipped settings, "
            "not live (%d): %s. Template changed: %s. Feature flags changed (%d): %s.",
            SHIPPED_CODE,
            len(drift.wording_edited),
            ", ".join(drift.wording_edited),
            len(drift.wording_live_only),
            ", ".join(drift.wording_live_only),
            len(drift.wording_default_only),
            ", ".join(drift.wording_default_only),
            drift.template_changed,
            len(drift.features_changed),
            ", ".join(drift.features_changed),
        )
        record_event(
            EVENT_SETTINGS_DRIFT_DETECTED,
            {
                ATTR_SETTINGS_WORDING_EDITED_COUNT: len(drift.wording_edited),
                ATTR_SETTINGS_LIVE_ONLY_COUNT: len(drift.wording_live_only),
                ATTR_SETTINGS_DEFAULT_ONLY_COUNT: len(drift.wording_default_only),
                ATTR_SETTINGS_TEMPLATE_CHANGED: drift.template_changed,
                ATTR_SETTINGS_FEATURES_CHANGED_COUNT: len(drift.features_changed),
            },
        )
        return drift
    except Exception as exc:
        # The type only: a database error message can carry the connection string.
        logger.warning("Jurisdiction settings drift check skipped: %s", type(exc).__name__)
        return None

"""Report where the live document content differs from the bundled defaults.

Admins edit the wording in the live service (the document_content row); a new
software version may ship different defaults in data/document_content.json. The
live row always wins: nothing here, and nothing at deployment, writes to it. This
check only tells the product team, once at startup, which items differ so they can
decide whether a change in the defaults needs applying by hand.

Items are matched by id in hearing_types, legal_frameworks and anonymity.statuses.
Only ids and counts are reported, never the wording.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from transcribe_api.documents.content_loader import _load_from_file

logger = logging.getLogger(__name__)

# Collection name -> path to its list of items in the content payload.
COLLECTIONS: dict[str, tuple[str, ...]] = {
    "hearing_types": ("hearing_types",),
    "legal_frameworks": ("legal_frameworks",),
    "anonymity_statuses": ("anonymity", "statuses"),
}


@dataclass
class ContentDrift:
    """Item ids as "<collection>/<id>"."""

    edited: list[str] = field(default_factory=list)  # in both, live differs from the defaults
    live_only: list[str] = field(default_factory=list)  # added in the live service
    default_only: list[str] = field(default_factory=list)  # shipped, missing live: did not propagate

    def __bool__(self) -> bool:
        return bool(self.edited or self.live_only or self.default_only)


def _items_by_id(content: Any, path: tuple[str, ...]) -> dict[str, Any]:
    for key in path:
        content = content.get(key) if isinstance(content, dict) else None
    if not isinstance(content, list):
        return {}
    return {str(item["id"]): item for item in content if isinstance(item, dict) and "id" in item}


def compare_content(live: dict[str, Any], shipped: dict[str, Any]) -> ContentDrift:
    drift = ContentDrift()
    for name, path in COLLECTIONS.items():
        live_items = _items_by_id(live, path)
        shipped_items = _items_by_id(shipped, path)
        drift.edited += [
            f"{name}/{i}" for i in sorted(live_items.keys() & shipped_items.keys()) if live_items[i] != shipped_items[i]
        ]
        drift.live_only += [f"{name}/{i}" for i in sorted(live_items.keys() - shipped_items.keys())]
        drift.default_only += [f"{name}/{i}" for i in sorted(shipped_items.keys() - live_items.keys())]
    return drift


def check_content_drift() -> ContentDrift | None:
    """Warn and record content.drift_detected when the live content differs from the defaults.

    Read-only, and never raises: a failure here must not stop startup. Returns the
    drift, or None when there is none or the check could not run.
    """
    try:
        from sqlmodel import Session, select

        from transcribe_api.domain.models_dictation import DocumentContent
        from transcribe_api.runtime.db_connection import get_engine

        with Session(get_engine()) as session:
            row = session.exec(select(DocumentContent).where(DocumentContent.id == 1)).first()
            live = row.content if row is not None else None
        if live is None:
            return None  # Nothing live yet: the first request seeds it from the defaults.

        drift = compare_content(live, _load_from_file())
        if not drift:
            return None

        from hmcts_otel import record_event

        from transcribe_api.runtime.telemetry import (
            ATTR_CONTENT_DEFAULT_ONLY_COUNT,
            ATTR_CONTENT_EDITED_COUNT,
            ATTR_CONTENT_LIVE_ONLY_COUNT,
            EVENT_CONTENT_DRIFT_DETECTED,
        )

        logger.warning(
            "Live document content differs from the bundled defaults; the live content is kept. "
            "Edited live (%d): %s. Only live (%d): %s. Only in defaults, not applied (%d): %s.",
            len(drift.edited),
            ", ".join(drift.edited),
            len(drift.live_only),
            ", ".join(drift.live_only),
            len(drift.default_only),
            ", ".join(drift.default_only),
        )
        record_event(
            EVENT_CONTENT_DRIFT_DETECTED,
            {
                ATTR_CONTENT_EDITED_COUNT: len(drift.edited),
                ATTR_CONTENT_LIVE_ONLY_COUNT: len(drift.live_only),
                ATTR_CONTENT_DEFAULT_ONLY_COUNT: len(drift.default_only),
            },
        )
        return drift
    except Exception as exc:
        # The type only: a database error message can carry the connection string.
        logger.warning("Document content drift check skipped: %s", type(exc).__name__)
        return None

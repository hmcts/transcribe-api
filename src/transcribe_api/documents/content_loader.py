"""Content loader for document generation.

This module provides functions to load document content data from the
JSON configuration file, including hearing types, legal frameworks,
and anonymity content.

Content fields are stored as Markdown.  Helper functions expose both
the raw Markdown strings (for APIs / editing) and parsed Word-document
blocks (for the rendering pipeline).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from transcribe_api.documents.lib.markdown_to_blocks import markdown_to_blocks, markdown_to_plain_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from transcribe_api.documents.lib.models import ContentBlock

# MERGE NOTE: was three parents up (backend/) under the old layout, which
# resolved to src/ here. data/ now ships inside the package so it is present
# in a wheel/container too.
BASE_DIR = Path(__file__).resolve().parent.parent
# Bundled fallback — only used if the database row doesn't exist yet
CONTENT_FILE = BASE_DIR / "data" / "document_content.json"


class ContentLoadError(Exception):
    """Raised when content cannot be loaded."""


def _load_content_file() -> dict[str, Any]:
    """Load document content from the database.

    Falls back to the bundled JSON file if the database row has not been
    seeded yet (e.g. during initial migration or local development before
    running migrations).
    """
    from sqlmodel import Session, select

    from transcribe_api.runtime.db_connection import get_engine
    from transcribe_api.domain.models_dictation import DocumentContent

    try:
        with Session(get_engine()) as session:
            row = session.exec(select(DocumentContent).where(DocumentContent.id == 1)).first()
            if row is not None:
                return row.content

            # No row yet — read the file on this instance and seed the DB.
            # On existing deployments the file contains admin-modified content;
            # this ensures the first request after migration preserves it.
            content = _load_from_file()
            try:
                from datetime import UTC, datetime
                session.add(DocumentContent(id=1, content=content, updated_at=datetime.now(UTC)))
                session.commit()
            except Exception as seed_exc:
                session.rollback()
                logger.warning("Failed to seed document_content table: %s", seed_exc)
            return content
    except Exception:
        logger.exception(
            "Database unavailable for document content — falling back to local file. "
            "Reads may be inconsistent across instances.",
        )

    return _load_from_file()


def _load_from_file() -> dict[str, Any]:
    if not CONTENT_FILE.exists():
        msg = f"Content file not found: {CONTENT_FILE}"
        raise ContentLoadError(msg)
    try:
        with CONTENT_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in content file: {e}"
        raise ContentLoadError(msg) from e


_cache: dict[str, Any] = {"content": None, "updated_at": None}


def get_document_content() -> dict[str, Any]:
    """Get the full editable document content payload.

    Uses the database row's updated_at timestamp as a cache key. Every call
    does a cheap SELECT updated_at (single PK lookup, no JSONB) and compares
    it to the cached value. If they match the in-process copy is returned
    immediately; if they differ the full row is fetched and the cache is
    refreshed. This means every instance sees an admin save on its very next
    request — no fixed TTL, no stale data window.
    """
    from sqlmodel import Session, select

    from transcribe_api.runtime.db_connection import get_engine
    from transcribe_api.domain.models_dictation import DocumentContent

    try:
        with Session(get_engine()) as session:
            # Cheap check: fetch only the timestamp column
            ts_row = session.exec(
                select(DocumentContent.updated_at).where(DocumentContent.id == 1)
            ).first()

            if ts_row is not None and ts_row == _cache["updated_at"] and _cache["content"] is not None:
                return _cache["content"]

            # Timestamp changed or no cache — fetch full content
            row = session.exec(select(DocumentContent).where(DocumentContent.id == 1)).first()
            if row is not None:
                _cache["content"] = row.content
                _cache["updated_at"] = row.updated_at
                return _cache["content"]

            # No DB row yet — seed from file and persist
            content = _load_from_file()
            try:
                from datetime import UTC
                from datetime import datetime as dt
                now = dt.now(UTC)
                session.add(DocumentContent(id=1, content=content, updated_at=now))
                session.commit()
                _cache["content"] = content
                _cache["updated_at"] = now
            except Exception as seed_exc:
                session.rollback()
                logger.warning("Failed to seed document_content table: %s", seed_exc)
            return content
    except Exception:
        logger.exception(
            "Database unavailable for document content — falling back to local file. "
            "Reads may be inconsistent across instances.",
        )

    return _load_from_file()


def get_hearing_types() -> list[dict[str, str]]:
    """Get all hearing type definitions.

    Returns:
        List of hearing type dictionaries with keys:
        - id: Unique identifier
        - title: Display title
        - hearing_title: Title for the hearing section
        - hearing_description: Description text for the document
    """
    content = get_document_content()
    return content.get("hearing_types", [])


def get_hearing_type_by_id(hearing_type_id: str) -> dict[str, str] | None:
    """Get a specific hearing type by ID.

    Args:
        hearing_type_id: The hearing type identifier.

    Returns:
        The hearing type dictionary, or None if not found.
    """
    for ht in get_hearing_types():
        if ht.get("id") == hearing_type_id:
            return ht
    return None


def get_hearing_type_by_title(title: str) -> dict[str, str] | None:
    """Get a hearing type by its display title.

    Args:
        title: The hearing type title (case-insensitive).

    Returns:
        The hearing type dictionary, or None if not found.
    """
    title_lower = title.lower().strip()
    for ht in get_hearing_types():
        if ht.get("title", "").lower().strip() == title_lower:
            return ht
    return None


def get_legal_frameworks() -> list[dict[str, Any]]:
    """Get all legal framework definitions.

    Returns:
        List of legal framework dictionaries with keys:
        - id: Unique identifier
        - title: Display title
        - rank: Display order (lower = higher priority)
        - content: Main content text
        - issues: List of related legal issues/questions to consider
    """
    content = get_document_content()
    return content.get("legal_frameworks", [])


def get_legal_framework_by_id(framework_id: str) -> dict[str, Any] | None:
    """Get a specific legal framework by ID.

    Args:
        framework_id: The framework identifier.

    Returns:
        The framework dictionary, or None if not found.
    """
    for lf in get_legal_frameworks():
        if lf.get("id") == framework_id:
            return lf
    return None


def get_legal_framework_by_title(title: str) -> dict[str, Any] | None:
    """Get a legal framework by its display title.

    Args:
        title: The framework title (case-insensitive).

    Returns:
        The framework dictionary, or None if not found.
    """
    title_lower = title.lower().strip()
    for lf in get_legal_frameworks():
        if lf.get("title", "").lower().strip() == title_lower:
            return lf
    return None


def get_anonymity_statuses() -> list[dict[str, str]]:
    """Get all anonymity status definitions.

    Returns:
        List of anonymity status dictionaries with keys:
        - id: Status identifier (granted, sought_but_refused, not_sought)
        - title: Display title
        - content: Main content text
        - identifier: Short identifier text
        - header: Header text (or [DELETE])
        - order_header: Order section header (or [DELETE])
        - order_text: Full order text (or [DELETE])
    """
    content = get_document_content()
    return content.get("anonymity", {}).get("statuses", [])


def get_anonymity_status_by_id(status_id: str) -> dict[str, str] | None:
    """Get a specific anonymity status by ID.

    Args:
        status_id: The status identifier.

    Returns:
        The status dictionary, or None if not found.
    """
    for status in get_anonymity_statuses():
        if status.get("id") == status_id:
            return status
    return None


def clear_cache() -> None:
    """Reset the cached timestamp so the next read re-checks the database."""
    _cache["content"] = None
    _cache["updated_at"] = None


def get_hearing_description(hearing_type_id_or_title: str) -> str:
    """Get the hearing description Markdown for a given hearing type.

    Args:
        hearing_type_id_or_title: The hearing type identifier (e.g., "face_to_face")
            or display title (e.g., "Face to face").

    Returns:
        The hearing description as Markdown, or a placeholder if not found.
    """
    hearing_type = get_hearing_type_by_id(hearing_type_id_or_title)
    if not hearing_type:
        hearing_type = get_hearing_type_by_title(hearing_type_id_or_title)
    if hearing_type:
        return hearing_type.get("hearing_description", f"[{hearing_type_id_or_title}]")
    return f"[Hearing type: {hearing_type_id_or_title}]"


def get_hearing_description_blocks(hearing_type_id_or_title: str) -> list[ContentBlock]:
    """Parse the hearing description Markdown into Word blocks."""
    md = get_hearing_description(hearing_type_id_or_title)
    return markdown_to_blocks(md)


def get_hearing_title(hearing_type_id_or_title: str) -> str:
    """Get the hearing section title for a given hearing type.

    Args:
        hearing_type_id_or_title: The hearing type identifier or display title.

    Returns:
        The hearing title (e.g., "The Hearing" or "Proceeding Without A Hearing").
    """
    # Try ID first, then title
    hearing_type = get_hearing_type_by_id(hearing_type_id_or_title)
    if not hearing_type:
        hearing_type = get_hearing_type_by_title(hearing_type_id_or_title)
    if hearing_type:
        return hearing_type.get("hearing_title", "The Hearing")
    return "The Hearing"


def _resolve_legal_framework(id_or_title: str) -> dict[str, Any] | None:
    """Resolve a legal framework by ID or title.

    Args:
        id_or_title: Either a framework ID (e.g., "asylum_pre_naba")
            or display title (e.g., "Asylum Pre-NABA").

    Returns:
        The framework dictionary, or None if not found.
    """
    # Try ID first, then title
    framework = get_legal_framework_by_id(id_or_title)
    if not framework:
        framework = get_legal_framework_by_title(id_or_title)
    return framework


def _resolve_and_sort_frameworks(
    framework_ids_or_titles: list[str],
) -> list[dict[str, Any]]:
    """Resolve framework identifiers to dicts and sort by ``rank``.

    Frameworks are returned in the canonical display order defined by
    each framework's ``rank`` field.  Unresolvable identifiers are
    silently dropped.

    Args:
        framework_ids_or_titles: List of framework identifiers or display titles.

    Returns:
        Resolved framework dicts sorted by ascending ``rank``.
    """
    frameworks: list[dict[str, Any]] = []
    for id_or_title in framework_ids_or_titles:
        framework = _resolve_legal_framework(id_or_title)
        if framework:
            frameworks.append(framework)
    frameworks.sort(key=lambda f: f.get("rank", 999))
    return frameworks


def get_legal_framework_content(framework_ids_or_titles: list[str]) -> str:
    """Get the combined legal framework content as plain text.

    Markdown is stripped so the result can be placed directly into
    Word content controls that accept plain strings.

    Frameworks are sorted by their canonical ``rank`` so the content
    always appears in the same order regardless of user click order.

    Args:
        framework_ids_or_titles: List of framework identifiers or display titles.

    Returns:
        Combined plain-text content from all specified frameworks.
    """
    if not framework_ids_or_titles:
        return "[No legal frameworks selected]"

    frameworks = _resolve_and_sort_frameworks(framework_ids_or_titles)
    if not frameworks:
        return "[Legal frameworks not found]"

    parts = [markdown_to_plain_text(f.get("content", "")) for f in frameworks]
    return "\n\n".join(p for p in parts if p)


def get_legal_framework_content_list(framework_ids_or_titles: list[str]) -> list[str]:
    """Return each framework's raw Markdown content as a separate string.

    Each entry preserves the framework's Markdown structure so it
    can be parsed into blocks independently.
    """
    frameworks = _resolve_and_sort_frameworks(framework_ids_or_titles)
    return [f.get("content", "") for f in frameworks if f.get("content", "")]


def get_legal_framework_content_blocks(framework_ids_or_titles: list[str]) -> list[ContentBlock]:
    """Parse each framework's Markdown content into Word document blocks.

    Frameworks are sorted by rank.  Each framework's content is parsed
    independently and the results are concatenated.
    """
    frameworks = _resolve_and_sort_frameworks(framework_ids_or_titles)
    blocks: list[ContentBlock] = []
    for fw in frameworks:
        md = fw.get("content", "")
        if md:
            blocks.extend(markdown_to_blocks(md))
    return blocks


def get_legal_framework_issues(framework_ids_or_titles: list[str]) -> list[str]:
    """Get all issues from the specified legal frameworks.

    Frameworks without explicit issue entries are skipped.

    Args:
        framework_ids_or_titles: List of framework identifiers or display titles.

    Returns:
        Combined list of all legal issues from the specified frameworks.
    """
    issues = []
    for framework in _resolve_and_sort_frameworks(framework_ids_or_titles):
        framework_issues = [str(issue).strip() for issue in framework.get("issues", []) if str(issue).strip()]
        issues.extend(framework_issues)
    return issues


# Backwards compatibility alias
def get_legal_framework_questions(framework_ids_or_titles: list[str]) -> list[str]:
    """Deprecated: Use get_legal_framework_issues instead."""
    return get_legal_framework_issues(framework_ids_or_titles)


def get_legal_issues_display(framework_ids_or_titles: list[str]) -> str:
    """Get an enumerated list of legal issues for the document.

    Frameworks are sorted by their canonical ``rank``. Each framework
    contributes its ``issues`` entries. Frameworks without explicit
    issue entries are skipped.

    Output is formatted as::

        (a) Taking the claim at its highest, is there a Convention reason?
        (b) Considering the credibility of the account...

    Args:
        framework_ids_or_titles: List of framework identifiers or display titles.

    Returns:
        Newline-separated, letter-enumerated string of issues.
    """
    if not framework_ids_or_titles:
        return "[No legal issues selected]"

    frameworks = _resolve_and_sort_frameworks(framework_ids_or_titles)
    if not frameworks:
        return "[No legal issues selected]"

    issue_lines: list[str] = []
    for fw in frameworks:
        framework_issues = [str(issue).strip() for issue in fw.get("issues", []) if str(issue).strip()]
        issue_lines.extend(framework_issues)

    lines = []
    for i, issue in enumerate(issue_lines):
        letter = chr(ord("a") + i)
        lines.append(f"({letter}) {issue}")

    return "\n".join(lines)

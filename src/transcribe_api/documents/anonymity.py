"""Anonymity content generation for Word documents.

This module defines the logic for populating anonymity-related content controls
in hearing decision documents based on the anonymity order status.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from transcribe_api.documents.content_loader import get_anonymity_status_by_id

# Marker used in templates to indicate content should be deleted
DELETE_MARKER = "[DELETE]"


class AnonymityStatus(StrEnum):
    """Status of an anonymity order for a hearing."""

    GRANTED = "granted"
    SOUGHT_BUT_REFUSED = "sought_but_refused"
    NOT_SOUGHT = "not_sought"


@dataclass(frozen=True)
class AnonymityContent:
    """Content values for anonymity-related document fields.

    Attributes:
        reasons_text: Main body text explaining the anonymity decision
        order_identifier: Short identifier for the anonymity status (Yes/No/One was sought but refused)
        head: Header text shown at top of document when anonymity applies (e.g., "(ANONYMITY ORDER MADE)")
        reasons_heading: Section header for the anonymity reasons
        order_text: Full text of the anonymity order (rule 13(1)(b) text)

    Note: Field names map to Word template content controls:
        - AnonymityHead
        - AnonymityReasonsHeading
        - AnonymityReasonsText
        - AnonymityOrder
        - AnonymityOrderText
    """

    reasons_text: str
    order_identifier: str
    head: str
    reasons_heading: str
    order_text: str

    def to_template_dict(self) -> dict[str, str]:
        """Convert to a dictionary for template population.

        Returns:
            Dictionary mapping content control keys to values.
            Keys match the Word template content control tags.
        """
        # Format AnonymityOrder with prefix if identifier is present and not a delete marker
        if self.order_identifier and self.order_identifier != DELETE_MARKER:
            anonymity_order_value = f"Anonymity order made: {self.order_identifier}"
        else:
            anonymity_order_value = self.order_identifier

        return {
            "AnonymityHead": self.head,
            "AnonymityReasonsHeading": self.reasons_heading,
            "AnonymityReasonsText": self.reasons_text,
            "AnonymityOrder": anonymity_order_value,
            "AnonymityOrderText": self.order_text,
        }

    def get_delete_controls(self) -> set[str]:
        """Get the set of content control keys that should be deleted.

        Returns:
            Set of control keys marked for deletion.
        """
        delete_controls = set()
        mapping = self.to_template_dict()
        for key, value in mapping.items():
            if value == DELETE_MARKER:
                delete_controls.add(key)
        return delete_controls


def _load_anonymity_content(status: AnonymityStatus) -> AnonymityContent:
    """Load anonymity content from JSON configuration.

    Args:
        status: The anonymity status to load content for.

    Returns:
        AnonymityContent populated from the JSON file.

    Raises:
        ValueError: If the status is not found in the configuration.
    """
    status_data = get_anonymity_status_by_id(status.value)
    if status_data is None:
        msg = f"Anonymity status not found in configuration: {status.value}"
        raise ValueError(msg)

    return AnonymityContent(
        reasons_text=status_data.get("content", DELETE_MARKER),
        order_identifier=status_data.get("identifier", ""),
        head=status_data.get("header", DELETE_MARKER),
        reasons_heading=status_data.get("order_header", DELETE_MARKER),
        order_text=status_data.get("order_text", DELETE_MARKER),
    )


def get_anonymity_content(status: AnonymityStatus) -> AnonymityContent:
    """Get the anonymity content for a given status.

    Args:
        status: The anonymity order status.

    Returns:
        AnonymityContent with all field values for the given status.

    Raises:
        ValueError: If status is not a valid AnonymityStatus.
    """
    return _load_anonymity_content(status)


def clear_cache() -> None:
    """No-op — anonymity content is sourced via get_anonymity_status_by_id,
    which reads through content_loader.get_document_content. Cache
    invalidation is handled there."""


def parse_anonymity_status(value: str | None) -> AnonymityStatus:
    """Parse a string value into an AnonymityStatus.

    Handles various input formats from forms/APIs.

    Args:
        value: String representation of anonymity status.
               Accepts: 'granted', 'yes', 'true', 'sought_but_refused',
                       'refused', 'not_sought', 'no', 'none', None, ''

    Returns:
        The corresponding AnonymityStatus enum value.
    """
    if value is None:
        return AnonymityStatus.NOT_SOUGHT

    normalised = value.strip().lower()

    # Map various input values to status
    if normalised in ("granted", "yes", "true", "1"):
        return AnonymityStatus.GRANTED
    if normalised in ("sought_but_refused", "refused", "one was sought but refused"):
        return AnonymityStatus.SOUGHT_BUT_REFUSED
    if normalised in ("not_sought", "no", "none", "false", "0", ""):
        return AnonymityStatus.NOT_SOUGHT

    # Try to match enum value directly
    try:
        return AnonymityStatus(normalised)
    except ValueError:
        # Default to not sought for unknown values
        return AnonymityStatus.NOT_SOUGHT

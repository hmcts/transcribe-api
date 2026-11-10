"""A jurisdiction's uploaded Word templates (DIAAT-344): what the admin area does.

The file and its fields are checked here (documents/uploaded_templates.py); the routes
are in api/routes_dictation_privileged.py (SystemAdministrator) and
api/routes_jurisdictions.py (the judges' list). Errors are jurisdictions_admin's, so
the admin routes map them the same way: a refusal 400, an unknown template 404, a
change made meanwhile 409.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.documents.uploaded_templates import (
    NOT_ASKED,
    Box,
    TemplateField,
    check_template_file,
    field_problems,
    find_boxes,
    merge_fields,
    normalise_fields,
    propose_field,
)
from transcribe_api.domain.models_dictation import JurisdictionTemplate
from transcribe_api.jurisdictions_admin import AdminError, ConflictError, NotFoundError, _jurisdiction, version

MAX_NAME_LENGTH = 200


class TemplateProblemsError(AdminError):
    """The fields cannot be saved yet; ``problems`` names each box or field."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("The template cannot be saved yet. " + " ".join(problems))


def boxes_of(row: JurisdictionTemplate) -> list[Box]:
    return [Box.from_record(b) for b in row.boxes]


def fields_of(row: JurisdictionTemplate) -> list[TemplateField]:
    return [TemplateField.model_validate(f) for f in row.fields]


def problems_of(row: JurisdictionTemplate) -> list[str]:
    return field_problems(boxes_of(row), fields_of(row))


def _name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        msg = f"The template's name must be 1 to {MAX_NAME_LENGTH} characters."
        raise AdminError(msg)
    return name


def _template(session: Session, code: str, template_id: UUID, *, lock: bool = False) -> JurisdictionTemplate:
    jurisdiction = _jurisdiction(session, code)
    query = select(JurisdictionTemplate).where(
        JurisdictionTemplate.id == template_id, JurisdictionTemplate.jurisdiction_id == jurisdiction.id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.exec(query).first()
    if row is None:
        msg = f"No such template in {jurisdiction.code}"
        raise NotFoundError(msg)
    return row


def _commit(session: Session, name: str) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        msg = f"There is already a template called “{name}” in this jurisdiction."
        raise AdminError(msg) from None


def _read_file(data: bytes) -> list[Box]:
    """The file's boxes; a file that cannot be used is refused (AdminError)."""
    from transcribe_api.documents.uploaded_templates import TemplateFileError

    try:
        check_template_file(data)
        return find_boxes(data)
    except TemplateFileError as exc:
        raise AdminError(str(exc)) from None


def _default_name(file_name: str) -> str:
    stem = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.lower().endswith(".docx"):
        stem = stem[:-5]
    return stem.strip()[:MAX_NAME_LENGTH] or "Template"


def add_template(session: Session, code: str, file_name: str, data: bytes) -> JurisdictionTemplate:
    """A new template from an upload, with a proposed field for each box. Not
    available to judges until the admin has checked its fields and enabled it."""
    jurisdiction = _jurisdiction(session, code)
    boxes = _read_file(data)
    name = _default_name(file_name)
    taken = set(
        session.exec(
            select(JurisdictionTemplate.name).where(JurisdictionTemplate.jurisdiction_id == jurisdiction.id)
        ).all()
    )
    unique, n = name, 2
    while unique in taken:
        suffix = f" ({n})"
        unique = f"{name[: MAX_NAME_LENGTH - len(suffix)]}{suffix}"
        n += 1
    row = JurisdictionTemplate(
        jurisdiction_id=jurisdiction.id,
        name=unique,
        file_name=file_name[:255] or "template.docx",
        content=data,
        sha256=hashlib.sha256(data).hexdigest(),
        boxes=[b.to_record() for b in boxes],
        fields=[propose_field(b).model_dump() for b in boxes],
        enabled=False,
    )
    session.add(row)
    _commit(session, unique)
    session.refresh(row)
    return row


def replace_file(
    session: Session, code: str, template_id: UUID, file_name: str, data: bytes, expected_version: str
) -> JurisdictionTemplate:
    """A new file for a template: fields kept for the boxes it still has; a new box
    has no field until the admin links one. Judges stop seeing it until it is complete."""
    boxes = _read_file(data)
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    fields = merge_fields(boxes, fields_of(row))
    row.file_name = file_name[:255] or row.file_name
    row.content = data
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.boxes = [b.to_record() for b in boxes]
    row.fields = [f.model_dump() for f in fields]
    if field_problems(boxes, fields):
        row.enabled = False
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    _commit(session, row.name)
    session.refresh(row)
    return row


def _check_version(row: JurisdictionTemplate, expected_version: str) -> None:
    if version(row) != expected_version:  # type: ignore[arg-type]
        msg = "The template was changed meanwhile. Reload it and make your change again."
        raise ConflictError(msg)


def update_template(
    session: Session,
    code: str,
    template_id: UUID,
    *,
    name: str,
    service: str | None,
    fields: list[TemplateField],
    enabled: bool,
    expected_version: str,
) -> JurisdictionTemplate:
    """Save the admin's fields: every box linked and every field complete, or nothing
    is saved (TemplateProblemsError names what is missing)."""
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    problems = field_problems(boxes_of(row), fields)
    if problems:
        raise TemplateProblemsError(problems)
    row.name = _name(name)
    row.service = (service or "").strip()[:MAX_NAME_LENGTH] or None
    row.fields = [f.model_dump() for f in normalise_fields(fields)]
    row.enabled = enabled
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    _commit(session, row.name)
    session.refresh(row)
    return row


def delete_template(session: Session, code: str, template_id: UUID) -> str:
    row = _template(session, code, template_id, lock=True)
    name = row.name
    session.delete(row)
    session.commit()
    return name


def templates_of(session: Session, code: str) -> list[JurisdictionTemplate]:
    jurisdiction = _jurisdiction(session, code)
    return list(
        session.exec(
            select(JurisdictionTemplate)
            .where(JurisdictionTemplate.jurisdiction_id == jurisdiction.id)
            .order_by(JurisdictionTemplate.name)
        ).all()
    )


def template_record(row: JurisdictionTemplate) -> dict[str, Any]:
    """The admin area's view of a template: everything but the file itself."""
    return {
        "id": str(row.id),
        "name": row.name,
        "service": row.service,
        "file_name": row.file_name,
        "enabled": row.enabled,
        "boxes": row.boxes,
        "fields": row.fields,
        "problems": problems_of(row),
        "version": version(row),  # type: ignore[arg-type]
    }


def available_templates(session: Session, jurisdiction_id: UUID) -> list[JurisdictionTemplate]:
    """What the jurisdiction's judges can choose: enabled, every box linked."""
    rows = session.exec(
        select(JurisdictionTemplate)
        .where(JurisdictionTemplate.jurisdiction_id == jurisdiction_id, JurisdictionTemplate.enabled.is_(True))  # type: ignore[union-attr,attr-defined]
        .order_by(JurisdictionTemplate.name)
    ).all()
    return [row for row in rows if not problems_of(row)]


def available_template(session: Session, jurisdiction_id: UUID, template_id: UUID) -> JurisdictionTemplate | None:
    row = session.exec(
        select(JurisdictionTemplate).where(
            JurisdictionTemplate.id == template_id,
            JurisdictionTemplate.jurisdiction_id == jurisdiction_id,
            JurisdictionTemplate.enabled.is_(True),  # type: ignore[union-attr,attr-defined]
        )
    ).first()
    return row if row is not None and not problems_of(row) else None


def judge_fields(row: JurisdictionTemplate) -> list[TemplateField]:
    """The fields a judge fills, in the admin's order."""
    return [f for f in fields_of(row) if f.type not in NOT_ASKED]

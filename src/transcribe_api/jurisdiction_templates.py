"""A jurisdiction's uploaded Word templates (DIAAT-344): what the admin area does.

The file and its fields are checked here (documents/uploaded_templates.py); the routes
are in api/routes_dictation_privileged.py (SystemAdministrator: managing them and,
DIAAT-346, previewing them) and api/routes_jurisdictions.py (the judges' list). Errors
are jurisdictions_admin's, so the admin routes map them the same way: a refusal 400,
an unknown template 404, a change made meanwhile 409.

DIAAT-347: a template goes from draft to the jurisdiction's test group to all its
judges (``stage``), live only with the service lead's sign-off (set_stage).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.documents.content_loader import get_document_content
from transcribe_api.documents.uploaded_templates import (
    NOT_ASKED,
    Box,
    FillContext,
    TemplateField,
    Value,
    box_text,
    check_template_file,
    check_values,
    field_problems,
    fill_template,
    find_boxes,
    merge_fields,
    normalise_fields,
    propose_field,
)
from transcribe_api.domain.auth.permissions import ROLE_JURISDICTION_ADMIN
from transcribe_api.domain.jurisdiction_stages import STAGE_DRAFT, STAGE_LIVE, STAGE_TEST, STAGES, sees_template
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionTemplate, RoleAssignment
from transcribe_api.jurisdictions_admin import (
    AdminError,
    ConflictError,
    NotFoundError,
    SignOff,
    _jurisdiction,
    set_stage,
    sign_off_record,
    version,
)
from transcribe_api.runtime.logger import logger

MAX_NAME_LENGTH = 200


class TemplateProblemsError(AdminError):
    """The fields cannot be saved yet; ``problems`` names each box or field."""

    def __init__(self, problems: list[str], *, action: str = "saved") -> None:
        self.problems = problems
        super().__init__(f"The template cannot be {action} yet. " + " ".join(problems))


class TemplateValuesError(AdminError):
    """The values do not fit the template's fields; ``problems`` names each."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("The hearing details do not fit the template. " + " ".join(problems))


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
    """A new template from an upload, with a proposed field for each box: a draft, not
    available to judges until the admin has checked its fields and moved it on."""
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
        stage=STAGE_DRAFT,
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
    has no field until the admin links one. Until it is complete it is a draft again,
    which judges do not see, and goes live again only with a new sign-off."""
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
        _move(row, STAGE_DRAFT, None)
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    _commit(session, row.name)
    session.refresh(row)
    return row


def _check_version(row: JurisdictionTemplate, expected_version: str) -> None:
    if version(row) != expected_version:  # type: ignore[arg-type]
        msg = "The template was changed meanwhile. Reload it and make your change again."
        raise ConflictError(msg)


def _move(row: JurisdictionTemplate, stage: str, sign_off: SignOff | None) -> None:
    set_stage(row, stage, sign_off)
    # For the previous release's pods while this one rolls out: they read ``enabled``.
    # What they write to it is kept to the stage rules by the database (0012).
    row.enabled = row.stage == STAGE_LIVE


def update_template(
    session: Session,
    code: str,
    template_id: UUID,
    *,
    name: str,
    service: str | None,
    fields: list[TemplateField],
    stage: str,
    expected_version: str,
    sign_off: SignOff | None = None,
) -> JurisdictionTemplate:
    """Save the admin's fields and who can choose it (``stage``): every box linked and
    every field complete, or nothing is saved (TemplateProblemsError names what is
    missing). It goes live only with ``sign_off`` (NotSignedOffError)."""
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    problems = field_problems(boxes_of(row), fields)
    if problems:
        raise TemplateProblemsError(problems)
    row.name = _name(name)
    row.service = (service or "").strip()[:MAX_NAME_LENGTH] or None
    row.fields = [f.model_dump() for f in normalise_fields(fields)]
    if stage not in STAGES:
        msg = f"The stage must be one of {', '.join(STAGES)}"
        raise AdminError(msg)
    _move(row, stage, sign_off)
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
        "stage": row.stage,
        "sign_off": sign_off_record(row),
        # Live; what the previous web release shows (DIAAT-344).
        "enabled": row.stage == STAGE_LIVE,
        "boxes": row.boxes,
        "fields": row.fields,
        "problems": problems_of(row),
        "version": version(row),  # type: ignore[arg-type]
    }


def in_test_group(session: Session, user_id: UUID, jurisdiction_id: UUID) -> bool:
    """Whether the user is in the jurisdiction's test group (DIAAT-347): one of their
    assignments there is marked tester, or they are its administrator. Whatever role
    they work as now: an administrator also works there as a Judge (DIAAT-373)."""
    rows = session.exec(
        select(RoleAssignment.role, RoleAssignment.tester).where(
            RoleAssignment.user_id == user_id, RoleAssignment.jurisdiction_id == jurisdiction_id
        )
    ).all()
    return any(tester or role == ROLE_JURISDICTION_ADMIN for role, tester in rows)


def available_templates(session: Session, jurisdiction_id: UUID, user_id: UUID) -> list[JurisdictionTemplate]:
    """What the user can choose in the jurisdiction (DIAAT-347): live ones, and those in
    test when they are in its test group, with every box linked."""
    rows = session.exec(
        select(JurisdictionTemplate)
        .where(JurisdictionTemplate.jurisdiction_id == jurisdiction_id, JurisdictionTemplate.stage != STAGE_DRAFT)
        .order_by(JurisdictionTemplate.name)
    ).all()
    testing = any(row.stage == STAGE_TEST for row in rows) and in_test_group(session, user_id, jurisdiction_id)
    return [row for row in rows if sees_template(row.stage, testing) and not problems_of(row)]


def available_template(
    session: Session, jurisdiction_id: UUID, template_id: UUID, user_id: UUID
) -> JurisdictionTemplate | None:
    row = session.exec(
        select(JurisdictionTemplate).where(
            JurisdictionTemplate.id == template_id,
            JurisdictionTemplate.jurisdiction_id == jurisdiction_id,
        )
    ).first()
    if row is None or problems_of(row):
        return None
    testing = row.stage == STAGE_TEST and in_test_group(session, user_id, jurisdiction_id)
    return row if sees_template(row.stage, testing) else None


def judge_fields(row: JurisdictionTemplate) -> list[TemplateField]:
    """The fields a judge fills, in the admin's order."""
    return [f for f in fields_of(row) if f.type not in NOT_ASKED]


# Filling a template: a judge's submission and the admin's preview (DIAAT-346) -----------


def legal_frameworks(jurisdiction_id: UUID) -> dict[str, str]:
    """The jurisdiction's legal frameworks, as its Legal Text Manager keeps them: id ->
    title, in their order. What a legal issues field offers."""
    frameworks = get_document_content(jurisdiction_id).get("legal_frameworks") or []
    return {
        str(f["id"]): str(f.get("title") or f["id"])
        for f in sorted(frameworks, key=lambda f: f.get("rank", 0))
        if f.get("id")
    }


def legal_issue_titles(jurisdiction_id: UUID, fields: list[TemplateField]) -> dict[str, str]:
    """legal_frameworks, looked up only when a field asks for legal issues."""
    return legal_frameworks(jurisdiction_id) if any(f.type == "legal_issues" for f in fields) else {}


def template_texts(
    fields: list[TemplateField],
    values: dict[str, Value],
    *,
    jurisdiction_id: UUID,
    jurisdiction_name: str,
    dictated: str,
) -> tuple[dict[str, Value], dict[str, str]]:
    """The values kept, and the text for each box. TemplateValuesError names each value
    that does not fit its field."""
    titles = legal_issue_titles(jurisdiction_id, fields)
    kept, problems = check_values(fields, values, list(titles))
    if problems:
        raise TemplateValuesError(problems)
    texts = box_text(
        fields,
        kept,
        FillContext(
            jurisdiction_name=jurisdiction_name,
            year=datetime.now(UTC).year,
            dictated_text=dictated,
            legal_issue_titles=titles,
        ),
    )
    return kept, texts


# What the preview puts where the judge's dictation goes.
PREVIEW_DICTATED_TEXT = (
    "This is where the judge's dictated text goes.\n\n"
    "Each paragraph the judge dictates or types appears here, in this box's style."
)


def template_of(session: Session, code: str, template_id: UUID) -> JurisdictionTemplate:
    """One of the jurisdiction's templates, whether or not judges can choose it."""
    return _template(session, code, template_id)


def preview_source(
    session: Session, code: str, template_id: UUID, expected_version: str
) -> tuple[JurisdictionTemplate, Jurisdiction]:
    """The template to preview and its jurisdiction, whether or not judges can choose
    it yet. TemplateProblemsError while a box has no complete field."""
    row = _template(session, code, template_id)
    _check_version(row, expected_version)
    problems = field_problems(boxes_of(row), fields_of(row))
    if problems:
        raise TemplateProblemsError(problems, action="previewed")
    return row, _jurisdiction(session, code)


def preview_template(
    row: JurisdictionTemplate, jurisdiction: Jurisdiction, values: dict[str, Value], directory: Path
) -> Path:
    """The template filled with the admin's sample values as a judge's submission fills
    it: the .docx, saved in ``directory`` (the caller's to remove), outside the session
    that read it, as the submission does. Refused in plain words: TemplateValuesError
    for values that do not fit, AdminError when the file cannot be filled."""
    _, texts = template_texts(
        fields_of(row),
        values,
        jurisdiction_id=jurisdiction.id,
        jurisdiction_name=jurisdiction.display_name or jurisdiction.name,
        dictated=PREVIEW_DICTATED_TEXT,
    )
    try:
        return fill_template(row.content, texts, directory / "preview.docx")
    except Exception:
        logger.exception("Template preview could not be filled")
        msg = (
            "BenchNotes could not fill this Word file, so judges would not get a document "
            "from it either. Check that it opens in Word, then upload it again."
        )
        raise AdminError(msg) from None

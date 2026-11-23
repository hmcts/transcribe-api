"""A jurisdiction's uploaded Word templates (DIAAT-344): what the admin area does.

The file and its fields are checked here (documents/uploaded_templates.py); the routes
are in api/routes_dictation_privileged.py (SystemAdministrator: managing them and,
DIAAT-346, previewing them) and api/routes_jurisdictions.py (the judges' list). Errors
are jurisdictions_admin's, so the admin routes map them the same way: a refusal 400,
an unknown template 404, a change made meanwhile 409.

DIAAT-347: a template goes from draft to the jurisdiction's test group to all its
judges (``stage``), live only with the service lead's sign-off (set_stage).

DIAAT-348: every save is kept as a version (setting_history.py), and an earlier one's
content can be saved again (restore_template).

DIAAT-336: a jurisdiction's admin can share a complete template as a base; an admin of
any jurisdiction can then start a template of their own from a copy of it
(add_from_base) and change its fields there. The copy is theirs: later changes to the
shared one do not change it.

DIAAT-339: a judge's work is not disrupted by changes to a template. A document is
filled once, when it is submitted, and kept as it was made. A draft keeps the version of
the template it was started with (its ``revision``): submitting it fills that version
from the history, even once the template has changed or been switched off, provided
the judge could choose that version (template_version).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer
from sqlmodel import Session, select

from transcribe_api import setting_history
from transcribe_api.documents.content_loader import get_document_content
from transcribe_api.documents.uploaded_templates import (
    NOT_ASKED,
    Box,
    BoxContent,
    FillContext,
    TemplateField,
    Value,
    box_text,
    check_template_file,
    check_values,
    dictation_sections,
    field_problems,
    fill_template,
    find_boxes,
    merge_fields,
    normalise_fields,
    propose_field,
)
from transcribe_api.domain.auth.permissions import ROLE_JURISDICTION_ADMIN
from transcribe_api.domain.jurisdiction_stages import STAGE_DRAFT, STAGE_LIVE, STAGE_TEST, STAGES, sees_template
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionTemplate, RoleAssignment, SettingVersion
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


def _unique_name(session: Session, jurisdiction_id: UUID, name: str) -> str:
    """``name``, or with " (2)", " (3)"... when the jurisdiction has a template called it."""
    taken = set(
        session.exec(
            select(JurisdictionTemplate.name).where(JurisdictionTemplate.jurisdiction_id == jurisdiction_id)
        ).all()
    )
    unique, n = name, 2
    while unique in taken:
        suffix = f" ({n})"
        unique = f"{name[: MAX_NAME_LENGTH - len(suffix)]}{suffix}"
        n += 1
    return unique


def add_template(
    session: Session, code: str, file_name: str, data: bytes, *, saved_by: UUID | None = None
) -> JurisdictionTemplate:
    """A new template from an upload, with a proposed field for each box: a draft, not
    available to judges until the admin has checked its fields and moved it on."""
    jurisdiction = _jurisdiction(session, code)
    boxes = _read_file(data)
    unique = _unique_name(session, jurisdiction.id, _default_name(file_name))
    row = JurisdictionTemplate(
        jurisdiction_id=jurisdiction.id,
        name=unique,
        file_name=file_name[:255] or "template.docx",
        content=data,
        sha256=hashlib.sha256(data).hexdigest(),
        boxes=[b.to_record() for b in boxes],
        fields=[propose_field(b).stored() for b in boxes],
        stage=STAGE_DRAFT,
        enabled=False,
    )
    session.add(row)
    setting_history.record(session, row, saved_by)
    _commit(session, unique)
    session.refresh(row)
    return row


def replace_file(
    session: Session,
    code: str,
    template_id: UUID,
    file_name: str,
    data: bytes,
    expected_version: str,
    *,
    saved_by: UUID | None = None,
) -> JurisdictionTemplate:
    """A new file for a template: fields kept for the boxes it still has; a new box
    has no field until the admin links one. Until it is complete it is a draft again,
    which judges do not see, and goes live again only with a new sign-off; and, if it
    was shared as a base, it is no longer shared until its admin shares it again. One
    started from a shared base no longer has the base's layout, so no longer says it did."""
    boxes = _read_file(data)
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    setting_history.catch_up(session, row)
    fields = merge_fields(boxes, fields_of(row))
    row.file_name = file_name[:255] or row.file_name
    row.content = data
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.boxes = [b.to_record() for b in boxes]
    row.fields = [f.stored() for f in fields]
    row.started_from = None
    if field_problems(boxes, fields):
        _move(row, STAGE_DRAFT, None)
        row.shared_as_base = False
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    setting_history.record(session, row, saved_by)
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
    shared_as_base: bool | None = None,
    saved_by: UUID | None = None,
) -> JurisdictionTemplate:
    """Save the admin's fields and who can choose it (``stage``): every box linked and
    every field complete, or nothing is saved (TemplateProblemsError names what is
    missing). It goes live only with ``sign_off`` (NotSignedOffError). ``shared_as_base``
    shares it with other jurisdictions or stops sharing it (DIAAT-336); None leaves it
    as it is."""
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    setting_history.catch_up(session, row)
    problems = field_problems(boxes_of(row), fields)
    if problems:
        raise TemplateProblemsError(problems)
    row.name = _name(name)
    row.service = (service or "").strip()[:MAX_NAME_LENGTH] or None
    row.fields = [f.stored() for f in normalise_fields(fields)]
    if stage not in STAGES:
        msg = f"The stage must be one of {', '.join(STAGES)}"
        raise AdminError(msg)
    _move(row, stage, sign_off)
    if shared_as_base is not None:
        row.shared_as_base = shared_as_base
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    setting_history.record(session, row, saved_by)
    _commit(session, row.name)
    session.refresh(row)
    return row


def restore_template(
    session: Session,
    code: str,
    template_id: UUID,
    number: int,
    *,
    expected_version: str,
    saved_by: UUID | None = None,
) -> JurisdictionTemplate:
    """Go back to version ``number`` (DIAAT-348): its name, file and fields saved again,
    as a new version. Who can choose the template (its stage and sign-off) stays as it is
    now, so judges get the earlier version from then on. One past draft must still have
    every box linked (TemplateProblemsError)."""
    row = _template(session, code, template_id, lock=True)
    _check_version(row, expected_version)
    earlier = setting_history.version_of(session, setting_history.KIND_TEMPLATE, row.id, number)
    if earlier is None:
        msg = f"The template has no version {number}"
        raise NotFoundError(msg)
    snapshot = earlier.snapshot
    content = row.content if snapshot["sha256"] == row.sha256 else setting_history.file_of(session, snapshot["sha256"])
    if content is None:
        msg = f"The file of version {number} is not kept"
        raise NotFoundError(msg)
    boxes = [Box.from_record(b) for b in snapshot["boxes"]]
    try:
        fields = [TemplateField.model_validate(f) for f in snapshot["fields"]]
    except ValueError as exc:
        msg = f"Version {number} cannot be put back: {exc}"
        raise AdminError(msg) from None
    problems = field_problems(boxes, fields)
    if problems and row.stage != STAGE_DRAFT:
        raise TemplateProblemsError(problems)
    setting_history.catch_up(session, row)
    row.name = _name(snapshot["name"])
    row.service = snapshot.get("service")
    row.file_name = snapshot["file_name"]
    row.content = content
    row.sha256 = snapshot["sha256"]
    row.boxes = snapshot["boxes"]
    row.fields = snapshot["fields"]
    row.updated_datetime = datetime.now(UTC)
    session.add(row)
    setting_history.record(session, row, saved_by, restored_from=number)
    _commit(session, row.name)
    session.refresh(row)
    return row


def shared_templates(session: Session) -> list[tuple[JurisdictionTemplate, Jurisdiction]]:
    """The templates shared as a base (DIAAT-336), each with its jurisdiction: complete
    ones only, so a copy starts from one that works."""
    rows = session.exec(
        select(JurisdictionTemplate, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == JurisdictionTemplate.jurisdiction_id)  # type: ignore[arg-type]
        .where(JurisdictionTemplate.shared_as_base)
        .order_by(JurisdictionTemplate.name, Jurisdiction.code)
        # Listed by name: the files are not read.
        .options(defer(JurisdictionTemplate.content))  # type: ignore[arg-type]
    ).all()
    return [(row, jurisdiction) for row, jurisdiction in rows if not problems_of(row)]


def shared_template_record(row: JurisdictionTemplate, jurisdiction: Jurisdiction) -> dict[str, Any]:
    """What an admin chooses a base by."""
    return {
        "id": str(row.id),
        "name": row.name,
        "service": row.service,
        "jurisdiction": jurisdiction.code,
        "jurisdiction_name": jurisdiction.display_name or jurisdiction.name,
        "field_count": len(row.fields),
    }


def add_from_base(session: Session, code: str, base_id: UUID, *, saved_by: UUID | None = None) -> JurisdictionTemplate:
    """A new template of the jurisdiction from a copy of a shared one (DIAAT-336): its
    file, boxes, name and fields, as they are now. A draft, as an upload is, for the
    admin to change and move on; later changes to the shared one do not change it."""
    jurisdiction = _jurisdiction(session, code)
    shared = session.exec(
        select(JurisdictionTemplate, Jurisdiction)
        .join(Jurisdiction, Jurisdiction.id == JurisdictionTemplate.jurisdiction_id)  # type: ignore[arg-type]
        .where(JurisdictionTemplate.id == base_id, JurisdictionTemplate.shared_as_base)
    ).first()
    if shared is None:
        msg = "That template is no longer shared as a base."
        raise NotFoundError(msg)
    base, home = shared
    problems = problems_of(base)
    if problems:
        raise TemplateProblemsError(problems, action="copied")
    unique = _unique_name(session, jurisdiction.id, base.name)
    row = JurisdictionTemplate(
        jurisdiction_id=jurisdiction.id,
        name=unique,
        file_name=base.file_name,
        content=base.content,
        sha256=base.sha256,
        boxes=base.boxes,
        fields=base.fields,
        stage=STAGE_DRAFT,
        enabled=False,
        started_from={
            "template_id": str(base.id),
            "name": base.name,
            "jurisdiction": home.code,
            "jurisdiction_name": home.display_name or home.name,
            "copied_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(row)
    setting_history.record(session, row, saved_by)
    _commit(session, unique)
    session.refresh(row)
    return row


def delete_template(session: Session, code: str, template_id: UUID) -> str:
    """Delete a template. Its history is kept, with it as it was last (catch_up), so a
    judge's draft started with it can still be finished (DIAAT-339)."""
    row = _template(session, code, template_id, lock=True)
    name = row.name
    setting_history.catch_up(session, row)
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
        # DIAAT-336.
        "shared_as_base": row.shared_as_base,
        "started_from": row.started_from,
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


def judge_fields(fields: list[TemplateField]) -> list[TemplateField]:
    """The fields a judge fills, in the admin's order."""
    return [f for f in fields if f.type not in NOT_ASKED]


# A version of a template as a judge's draft started with it (DIAAT-339) ------------------


def revision_of(sha256: str, fields: list[dict[str, Any]]) -> str:
    """What a template fills a document from, in short: its file and its fields. The
    same for a version whose name, stage or sign-off alone changed."""
    content = json.dumps({"sha256": sha256, "fields": fields}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()[:32]


@dataclass
class TemplateVersion:
    """A template as it is now or as it was: what a judge's form and document are made from."""

    id: UUID
    name: str
    service: str | None
    sha256: str
    fields: list[TemplateField]
    revision: str
    content: bytes | None = None

    @classmethod
    def of(cls, row: JurisdictionTemplate) -> TemplateVersion:
        return cls(
            id=row.id,
            name=row.name,
            service=row.service,
            sha256=row.sha256,
            fields=fields_of(row),
            revision=revision_of(row.sha256, row.fields),
            content=row.content,
        )


def template_version(
    session: Session, jurisdiction_id: UUID, template_id: UUID, user_id: UUID, revision: str | None
) -> TemplateVersion | None:
    """The template a judge's work is made from: as it is now and only while they can
    choose it, with no ``revision`` (a page from before DIAAT-339); else the version
    ``revision`` names, as its draft started with it. That is the one now while they can
    choose it, else an earlier one from the history: kept once the template has changed
    or been switched off, so long as they could choose that version then (live, or in
    test while they are in its test group) and it was complete. None when there is none
    they may use. The file is read with it."""
    current = available_template(session, jurisdiction_id, template_id, user_id)
    if current is not None and (revision is None or revision_of(current.sha256, current.fields) == revision):
        return TemplateVersion.of(current)
    if revision is None:
        return None
    versions = session.exec(
        select(SettingVersion)
        .where(
            SettingVersion.kind == setting_history.KIND_TEMPLATE,
            SettingVersion.object_id == template_id,
            SettingVersion.jurisdiction_id == jurisdiction_id,
        )
        .order_by(SettingVersion.number.desc())  # type: ignore[attr-defined]
    ).all()
    matching = [v.snapshot for v in versions if revision_of(v.snapshot["sha256"], v.snapshot["fields"]) == revision]
    testing: bool | None = None
    for snapshot in matching:
        stage = snapshot.get("stage")
        if stage == STAGE_TEST and testing is None:
            testing = in_test_group(session, user_id, jurisdiction_id)
        if not sees_template(stage, bool(testing)):
            continue
        # Every match has the same file and fields: one that cannot be used, none can.
        try:
            fields = [TemplateField.model_validate(f) for f in snapshot["fields"]]
        except ValueError:
            return None
        if field_problems([Box.from_record(b) for b in snapshot["boxes"]], fields):
            return None
        content = setting_history.file_of(session, snapshot["sha256"])
        if content is None:
            return None
        return TemplateVersion(
            id=template_id,
            name=snapshot["name"],
            service=snapshot.get("service"),
            sha256=snapshot["sha256"],
            fields=fields,
            revision=revision,
            content=content,
        )
    return None


# Filling a template: a judge's submission and the admin's preview (DIAAT-346) -----------


def _ordered_frameworks(jurisdiction_id: UUID) -> list[dict[str, Any]]:
    frameworks = get_document_content(jurisdiction_id).get("legal_frameworks") or []
    return [f for f in sorted(frameworks, key=lambda f: f.get("rank", 0)) if f.get("id")]


def legal_frameworks(jurisdiction_id: UUID) -> dict[str, str]:
    """The jurisdiction's legal frameworks, as its Legal Text Manager keeps them: id ->
    title, in their order. What a legal issues field offers."""
    return {str(f["id"]): str(f.get("title") or f["id"]) for f in _ordered_frameworks(jurisdiction_id)}


def legal_framework_content(
    jurisdiction_id: UUID, fields: list[TemplateField]
) -> tuple[dict[str, str], dict[str, str]]:
    """Each legal framework's title and its text (Markdown), by id in their order: looked
    up only when a field asks for legal issues."""
    if not any(f.type == "legal_issues" for f in fields):
        return {}, {}
    frameworks = _ordered_frameworks(jurisdiction_id)
    titles = {str(f["id"]): str(f.get("title") or f["id"]) for f in frameworks}
    texts = {str(f["id"]): str(f.get("content") or "") for f in frameworks}
    return titles, texts


def template_texts(
    fields: list[TemplateField],
    values: dict[str, Value],
    *,
    jurisdiction_id: UUID,
    jurisdiction_name: str,
    dictated: dict[str, str],
) -> tuple[dict[str, Value], dict[str, BoxContent]]:
    """The values kept, and what goes in each box. TemplateValuesError names each value
    that does not fit its field."""
    titles, framework_texts = legal_framework_content(jurisdiction_id, fields)
    kept, problems = check_values(fields, values, list(titles))
    if problems:
        raise TemplateValuesError(problems)
    texts = box_text(
        fields,
        kept,
        FillContext(
            jurisdiction_name=jurisdiction_name,
            year=datetime.now(UTC).year,
            dictated_texts=dictated,
            legal_issue_titles=titles,
            legal_framework_texts=framework_texts,
        ),
    )
    return kept, texts


def preview_dictated_texts(fields: list[TemplateField]) -> dict[str, str]:
    """What the preview puts where the judge's dictation goes: in each section's box,
    which section's (DIAAT-416)."""
    return {
        f.tag: (
            f"This is where the judge's dictation for “{f.label}” goes.\n\n"
            "Each paragraph the judge dictates appears here, in this box's style."
        )
        for f in dictation_sections(fields)
    }


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
        dictated=preview_dictated_texts(fields_of(row)),
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

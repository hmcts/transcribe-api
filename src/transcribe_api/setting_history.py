"""Who changed a jurisdiction's settings or a template, when, and what it was before;
going back to an earlier version (DIAAT-348).

Every save adds a version (SettingVersion, numbered from 1 for each jurisdiction and
template): a copy of what was saved, by whom and when. The history compares each
version with the one before. Going back saves an earlier version's content again, as a
new version; who can see it (its stage, whether it is on, its sign-off) stays as it is
now (jurisdictions_admin.restore_jurisdiction, jurisdiction_templates.restore_template).

A save that does not go through here (the previous release's pods while this one rolls
out, or the command line) is found at the next save: the row is first recorded as it is
then (catch_up), so what each version changed from is always what was really there.
History starts with each jurisdiction's and template's first save after DIAAT-348.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql, sqlite
from sqlmodel import Session, func, select

from transcribe_api.domain.models_dictation import (
    Jurisdiction,
    JurisdictionTemplate,
    SettingVersion,
    TemplateFile,
    User,
)

KIND_JURISDICTION = "jurisdiction"
KIND_TEMPLATE = "template"

# What going back puts back: the content. Who can see it stays as it is now.
RESTORED = {
    KIND_JURISDICTION: ("name", "display_name", "service", "settings"),
    KIND_TEMPLATE: ("name", "service", "file_name", "sha256", "boxes", "fields"),
}


def kind_of(row: Jurisdiction | JurisdictionTemplate) -> str:
    return KIND_TEMPLATE if isinstance(row, JurisdictionTemplate) else KIND_JURISDICTION


def _jurisdiction_id(row: Jurisdiction | JurisdictionTemplate) -> UUID:
    return row.jurisdiction_id if isinstance(row, JurisdictionTemplate) else row.id


def snapshot_of(row: Jurisdiction | JurisdictionTemplate) -> dict[str, Any]:
    """What a version keeps: everything an admin sets. A template's file by its SHA-256
    (template_file)."""
    common = {"name": row.name, "service": row.service, "stage": row.stage, "signed_off_by": row.signed_off_by}
    if isinstance(row, JurisdictionTemplate):
        return {
            **common,
            "file_name": row.file_name,
            "sha256": row.sha256,
            "boxes": row.boxes,
            "fields": row.fields,
        }
    return {**common, "display_name": row.display_name, "enabled": row.enabled, "settings": row.settings or {}}


def _latest(session: Session, kind: str, object_id: UUID) -> SettingVersion | None:
    return session.exec(
        select(SettingVersion)
        .where(SettingVersion.kind == kind, SettingVersion.object_id == object_id)
        .order_by(SettingVersion.number.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()


def _add(
    session: Session,
    row: Jurisdiction | JurisdictionTemplate,
    *,
    saved_by: UUID | None,
    saved_at: datetime,
    restored_from: int | None = None,
) -> None:
    kind = kind_of(row)
    snapshot = snapshot_of(row)
    if isinstance(row, JurisdictionTemplate):
        _keep_file(session, row.sha256, row.content)
    number = session.exec(
        select(func.coalesce(func.max(SettingVersion.number), 0)).where(
            SettingVersion.kind == kind, SettingVersion.object_id == row.id
        )
    ).one()
    session.add(
        SettingVersion(
            kind=kind,
            object_id=row.id,
            jurisdiction_id=_jurisdiction_id(row),
            number=number + 1,
            snapshot=snapshot,
            saved_by=saved_by,
            saved_at=saved_at,
            restored_from=restored_from,
        )
    )


def _keep_file(session: Session, sha256: str, content: bytes) -> None:
    """Keep a template file once: the same file saved at the same moment by another
    admin is not a clash (it is the same bytes)."""
    insert = postgresql.insert if session.get_bind().dialect.name == "postgresql" else sqlite.insert
    session.execute(insert(TemplateFile).values(sha256=sha256, content=content).on_conflict_do_nothing())


def catch_up(session: Session, row: Jurisdiction | JurisdictionTemplate) -> None:
    """Before a save, under the row's lock: record the row as it is, if the history does
    not have it (its first save since DIAAT-348, or a save that did not come through
    here), with no one as the saver and the time it last changed. Written at once (the
    row is not changed yet), so the save's own version comes after it."""
    latest = _latest(session, kind_of(row), row.id)
    if latest is None or latest.snapshot != snapshot_of(row):
        _add(session, row, saved_by=None, saved_at=row.updated_datetime or datetime.now(UTC))
        session.flush()


def record(
    session: Session,
    row: Jurisdiction | JurisdictionTemplate,
    saved_by: UUID | None,
    *,
    restored_from: int | None = None,
) -> None:
    """After a save's changes, before its commit: the new version. A save that changed
    nothing adds none. Written by the save's commit, with it: a lookup here writes nothing
    first (a clash is then the commit's, as without the history)."""
    with session.no_autoflush:
        latest = _latest(session, kind_of(row), row.id)
        if latest is None or latest.snapshot != snapshot_of(row):
            _add(session, row, saved_by=saved_by, saved_at=datetime.now(UTC), restored_from=restored_from)


def version_of(session: Session, kind: str, object_id: UUID, number: int) -> SettingVersion | None:
    return session.exec(
        select(SettingVersion).where(
            SettingVersion.kind == kind, SettingVersion.object_id == object_id, SettingVersion.number == number
        )
    ).first()


def file_of(session: Session, sha256: str) -> bytes | None:
    stored = session.get(TemplateFile, sha256)
    return stored.content if stored else None


def _flat(kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """A version's settings one by one, as the history names them: ``settings.wording.<key>``,
    ``fields.<tag>.<what>``. A template's file is its name and the start of its SHA-256,
    so a new file of the same name shows."""
    flat = {key: snapshot.get(key) for key in ("name", "service", "stage", "signed_off_by")}
    if kind == KIND_TEMPLATE:
        flat["file"] = f"{snapshot.get('file_name')} ({str(snapshot.get('sha256'))[:8]})"
        fields = snapshot.get("fields") or []
        flat["field_order"] = [f.get("tag") for f in fields]
        for f in fields:
            for what, value in f.items():
                if what != "tag":
                    flat[f"fields.{f.get('tag')}.{what}"] = value
        return flat
    flat["display_name"] = snapshot.get("display_name")
    flat["enabled"] = snapshot.get("enabled")
    settings = snapshot.get("settings") or {}
    for key, value in settings.items():
        if isinstance(value, dict):
            for inner, text in value.items():
                flat[f"settings.{key}.{inner}"] = text
        else:
            flat[f"settings.{key}"] = value
    return flat


def changes(kind: str, before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    """What a version changed from the one before: each setting with what it was and
    what it became. None for the first version: nothing is known from before it."""
    old, new = _flat(kind, before or {}), _flat(kind, after)
    return [
        {"setting": key, "before": old.get(key), "after": new.get(key)}
        for key in [*new, *(k for k in old if k not in new)]
        if old.get(key) != new.get(key)
    ]


def history(session: Session, row: Jurisdiction | JurisdictionTemplate) -> list[dict[str, Any]]:
    """Its versions, newest first: who saved each and when, and what it changed."""
    kind = kind_of(row)
    versions = session.exec(
        select(SettingVersion)
        .where(SettingVersion.kind == kind, SettingVersion.object_id == row.id)
        .order_by(SettingVersion.number)  # type: ignore[arg-type]
    ).all()
    savers = {v.saved_by for v in versions if v.saved_by is not None}
    users = {u.id: u for u in session.exec(select(User).where(User.id.in_(savers))).all()} if savers else {}  # type: ignore[union-attr]
    restored = RESTORED[kind]
    now = {key: value for key, value in snapshot_of(row).items() if key in restored}
    out = []
    previous: SettingVersion | None = None
    for v in versions:
        user = users.get(v.saved_by) if v.saved_by else None
        out.append(
            {
                "number": v.number,
                "saved_at": v.saved_at,
                "saved_by": {"id": str(user.id), "email": user.email, "name": user.name} if user else None,
                "restored_from": v.restored_from,
                "first": previous is None,
                # Going back to it would change nothing: its content is what is there now.
                "content_as_now": {key: v.snapshot.get(key) for key in restored} == now,
                "changes": [] if previous is None else changes(kind, previous.snapshot, v.snapshot),
            }
        )
        previous = v
    return out[::-1]

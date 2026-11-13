"""DIAAT-348 on a real PostgreSQL (TEST_POSTGRES_URL, a throwaway one): versions kept
and put back through migration 0015's tables, with JSONB copies and template files
stored once. Skipped without a database (the route tests run on SQLite)."""

from __future__ import annotations

from sqlmodel import Session, func, select

from tests.unit.template_docx import JUDGMENT, make_template
from transcribe_api import jurisdiction_templates, jurisdictions_admin, setting_history
from transcribe_api.domain.models_dictation import Jurisdiction, SettingVersion, TemplateFile


def _versions(engine, row) -> list[dict]:
    with Session(engine) as session:
        return setting_history.history(session, session.get(type(row), row.id))


def test_a_jurisdiction_is_kept_and_put_back(fresh_postgres):
    with Session(fresh_postgres) as session:
        jurisdictions_admin.put_jurisdiction(
            session, "ET", "Employment Tribunal", {"wording": {"help.subtitle": "One"}}
        )
        jurisdictions_admin.set_wording(session, "ET", {"help.subtitle": "Two"})
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()

    assert [v["number"] for v in _versions(fresh_postgres, et)] == [2, 1]
    with Session(fresh_postgres) as session:
        current = jurisdictions_admin.version(session.get(Jurisdiction, et.id))
        restored = jurisdictions_admin.restore_jurisdiction(session, "ET", 1, expected_version=current)
        assert restored.settings["wording"] == {"help.subtitle": "One"}
    newest = _versions(fresh_postgres, et)[0]
    assert (newest["number"], newest["restored_from"], newest["content_as_now"]) == (3, 1, True)


def test_a_template_and_its_file_are_kept_once_and_put_back(fresh_postgres):
    first, second = make_template(JUDGMENT), make_template([*JUDGMENT, ["Signed"]])
    with Session(fresh_postgres) as session:
        jurisdictions_admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
        template = jurisdiction_templates.add_template(session, "ET", "One.docx", first)
        template = jurisdiction_templates.replace_file(
            session, "ET", template.id, "Two.docx", second, jurisdictions_admin.version(template)
        )
        template = jurisdiction_templates.restore_template(
            session, "ET", template.id, 1, expected_version=jurisdictions_admin.version(template)
        )
        assert (template.file_name, template.content) == ("One.docx", first)
        assert session.exec(select(func.count()).select_from(TemplateFile)).one() == 2
        assert session.exec(select(func.count()).select_from(SettingVersion)).one() == 4  # 1 ET, 3 template

#!/usr/bin/env python3
"""Seed the local database with the dev personas (DIAAT-373), so a developer can sign in as
each kind of user without Entra: ``POST /api/auth/session`` with ``X-Dev-Persona: <name>``
(only when ENVIRONMENT=local).

Creates, if missing, the jurisdictions ET and IAC, the personas' users
(``<name>@localhost.test``) and their role assignments:

    judge-et     Judge in ET                              jadmin-iac   JurisdictionAdministrator in IAC
    judge-iac    Judge in IAC                             jadmin-et    JurisdictionAdministrator in ET
    multi        Judge in ET, JurisdictionAdministrator   legal-iac    LegalTextManager in IAC
                 in IAC                                   jsuper       JurisdictionSuperAdministrator (global)
    sysadmin     no assignments, SystemAdministrator      no-role      no assignments

Safe to run again: what exists is left as it is, and nothing is deleted. It refuses to run
unless ENVIRONMENT is local and the database is the developer's own (the same check as the
other seed scripts: runtime/local_dev_database.py). Local development only.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("ENVIRONMENT", "local")

from sqlmodel import Session, create_engine, select

from transcribe_api.domain.assignments import ensure_assignment
from transcribe_api.domain.auth.dev_personas import PERSONA_JURISDICTIONS, PERSONAS
from transcribe_api.domain.jurisdictions import IAC_JURISDICTION_ID, JurisdictionSettings
from transcribe_api.domain.models_dictation import Jurisdiction, User
from transcribe_api.runtime.local_dev_database import (
    UnsafeDatabaseURLError,
    local_dev_database_url,
)

# The IAC exists on any migrated database (migration 0004, with its wording); this only
# covers one that lacks it. The ET has no template, and hearing recordings on, so its judge
# can try the recording pages.
_ET_SETTINGS = {"features": {"hearing_recordings": True}}


def _ensure_jurisdiction(session: Session, code: str, name: str) -> tuple[Jurisdiction, bool]:
    row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).first()
    if row is not None:
        return row, False
    settings = JurisdictionSettings.model_validate(_ET_SETTINGS if code == "ET" else {}).model_dump(mode="json")
    row = Jurisdiction(code=code, name=name, enabled=True, settings=settings)
    if code == "IAC":
        row.id = IAC_JURISDICTION_ID
    session.add(row)
    session.flush()
    return row, True


def seed(session: Session) -> dict[str, list[str]]:
    """Create what is missing; returns what was created. Flushed, not committed."""
    created: dict[str, list[str]] = {"jurisdictions": [], "users": [], "assignments": []}
    jurisdictions: dict[str, Jurisdiction] = {}
    for code, name in PERSONA_JURISDICTIONS.items():
        jurisdictions[code], made = _ensure_jurisdiction(session, code, name)
        if made:
            created["jurisdictions"].append(code)

    for persona in PERSONAS.values():
        user = session.exec(select(User).where(User.azure_user_id == persona.azure_user_id)).first()
        if user is None:
            user = User(email=persona.email, azure_user_id=persona.azure_user_id, name=persona.display_name)
            session.add(user)
            session.flush()
            created["users"].append(persona.name)
        for code, role in persona.assignments:
            _row, made = ensure_assignment(
                session,
                role=role,
                jurisdiction_id=jurisdictions[code].id if code else None,
                user_id=user.id,
            )
            if made:
                created["assignments"].append(f"{persona.name}: {role}@{code or 'global'}")
    return created


def main() -> int:
    if os.environ.get("ENVIRONMENT", "").lower() != "local":
        print("Refusing to run: ENVIRONMENT is not local.")
        return 1
    try:
        url = local_dev_database_url()
    except UnsafeDatabaseURLError as exc:
        print(f"Refusing to run against DATABASE_CONNECTION_STRING: {exc}. Check your .env.")
        return 1
    engine = create_engine(url)
    with Session(engine) as session:
        created = seed(session)
        session.commit()

    for kind, items in created.items():
        print(f"{kind}: {', '.join(items) if items else 'already there'}")
    print()
    print("Sign in with: curl -X POST localhost:8000/api/auth/session -H 'X-Dev-Persona: judge-et'")
    print(f"Personas: {', '.join(PERSONAS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Local development personas (DIAAT-373): who you can sign in as without Entra.

``POST /api/auth/session`` with ``X-Dev-Persona: <name>`` signs in as one of these, and only
when ENVIRONMENT=local: anywhere else the header is refused (401). ``scripts-seed-personas.py``
creates them (idempotently) with their jurisdictions and assignments, so a developer can
test "a Judge in ET cannot manage IAC users" with a few clicks. A persona goes through the
same mint path as a real sign-in (domain/auth/sessions.py): its assignments are read from the
database, and ``sysadmin`` stands in for the Entra SystemAdministrator role.

Emails are ``<name>@localhost.test``: a reserved test domain, so nobody real can be
addressed (and so nothing is emailed, DIAAT-398).
"""

from __future__ import annotations

from dataclasses import dataclass

from transcribe_api.domain.auth.permissions import (
    ROLE_JUDGE,
    ROLE_JURISDICTION_ADMIN,
    ROLE_JURISDICTION_SUPER_ADMIN,
    ROLE_LEGAL_TEXT_MANAGER,
)

EMAIL_DOMAIN = "localhost.test"

# Jurisdictions the personas hold roles in: code -> name. The IAC is the one migration 0004
# makes; the seed script creates the ET (and the IAC, if a database lacks it).
PERSONA_JURISDICTIONS: dict[str, str] = {
    "ET": "Employment Tribunal",
    "IAC": "Immigration and Asylum Chamber",
    "SSCS": "Social Security and Child Support",
}


@dataclass(frozen=True)
class Persona:
    name: str
    display_name: str
    # (jurisdiction code or None for global, role)
    assignments: tuple[tuple[str | None, str], ...]
    sysadmin: bool = False

    @property
    def email(self) -> str:
        return f"{self.name}@{EMAIL_DOMAIN}"

    @property
    def azure_user_id(self) -> str:
        """The user's identity in place of an Entra object ID: cannot collide with one."""
        return f"dev-persona-{self.name}"


def _persona(name: str, display_name: str, *assignments: tuple[str | None, str], sysadmin: bool = False) -> Persona:
    return Persona(name=name, display_name=display_name, assignments=assignments, sysadmin=sysadmin)


PERSONAS: dict[str, Persona] = {
    persona.name: persona
    for persona in (
        _persona("judge-et", "Judge (ET)", ("ET", ROLE_JUDGE)),
        _persona("judge-iac", "Judge (IAC)", ("IAC", ROLE_JUDGE)),
        _persona("multi", "Judge (ET) and Administrator (IAC)", ("ET", ROLE_JUDGE), ("IAC", ROLE_JURISDICTION_ADMIN)),
        _persona(
            "many",
            "Judge (IAC), Judge and Legal text manager (ET), Administrator (SSCS)",
            ("IAC", ROLE_JUDGE),
            ("ET", ROLE_JUDGE),
            ("ET", ROLE_LEGAL_TEXT_MANAGER),
            ("SSCS", ROLE_JURISDICTION_ADMIN),
        ),
        _persona("jadmin-iac", "Jurisdiction administrator (IAC)", ("IAC", ROLE_JURISDICTION_ADMIN)),
        _persona("jadmin-et", "Jurisdiction administrator (ET)", ("ET", ROLE_JURISDICTION_ADMIN)),
        _persona("legal-iac", "Legal text manager (IAC)", ("IAC", ROLE_LEGAL_TEXT_MANAGER)),
        _persona("jsuper", "Jurisdiction super administrator", (None, ROLE_JURISDICTION_SUPER_ADMIN)),
        _persona("sysadmin", "System administrator", sysadmin=True),
        _persona("no-role", "New user with no role"),
    )
}

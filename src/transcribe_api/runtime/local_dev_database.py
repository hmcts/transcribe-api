"""Database URL resolution for the local-only seed scripts (scripts-seed-*.py).

Those scripts delete and insert rows unconditionally, so they must only ever
reach a developer's own Postgres, whatever a misconfigured .env resolves to.
"""

from __future__ import annotations

import os

from sqlalchemy.engine import make_url

from transcribe_api.runtime.settings_recording import get_settings

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Allow-list rather than deny-list: libpq accepts many keys (host, hostaddr,
# port, service, dbname, options, ...) that redirect the connection away from
# the URL's own host.
_SAFE_QUERY_KEYS = frozenset({"sslmode", "connect_timeout", "application_name"})
# hostaddr from the environment overrides the URL's host, and a service entry
# can supply one.
_REDIRECTING_ENV_VARS = ("PGHOSTADDR", "PGSERVICE")


class UnsafeDatabaseURLError(ValueError):
    pass


def assert_local_database_url(url: str) -> None:
    parsed = make_url(url)
    unsafe_keys = sorted(set(parsed.query) - _SAFE_QUERY_KEYS)
    if unsafe_keys:
        raise UnsafeDatabaseURLError(
            f"query parameter(s) {unsafe_keys} are not allowed; only {sorted(_SAFE_QUERY_KEYS)} are"
        )
    if parsed.host not in _LOCAL_HOSTS:
        raise UnsafeDatabaseURLError(f"host {parsed.host!r} is not one of {sorted(_LOCAL_HOSTS)}")
    redirecting = [name for name in _REDIRECTING_ENV_VARS if os.environ.get(name)]
    if redirecting:
        raise UnsafeDatabaseURLError(f"environment variable(s) {redirecting} would redirect the connection")


def local_dev_database_url() -> str:
    """Resolve DATABASE_CONNECTION_STRING as the API does (including .env), refusing non-local targets."""
    url = get_settings().DATABASE_CONNECTION_STRING
    assert_local_database_url(url)
    return url

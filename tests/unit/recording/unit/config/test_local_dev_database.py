"""The localhost-only guard shared by the local seed scripts."""

import pytest

from transcribe_api.runtime.local_dev_database import (
    UnsafeDatabaseURLError,
    assert_local_database_url,
    local_dev_database_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost:5432/db",
        "postgresql://u:p@127.0.0.1/db",
        "postgresql://u:p@[::1]:5432/db",
        "postgresql+psycopg2://u:p@localhost/db?sslmode=disable&connect_timeout=5&application_name=seed",
    ],
)
def test_accepts_local_urls(url):
    assert_local_database_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@shared-db/db",
        "postgresql://u:p@localhost,shared-db/db",
        "postgresql://u:p@/db",
    ],
)
def test_rejects_non_local_hosts(url):
    with pytest.raises(UnsafeDatabaseURLError):
        assert_local_database_url(url)


@pytest.mark.parametrize(
    "query",
    [
        "host=shared-db",
        "host=localhost&host=shared-db",
        "hostaddr=10.0.0.5",
        "port=6543",
        "service=shared",
        "dbname=other",
        "options=-c%20search_path%3Dother",
        "target_session_attrs=read-write",
    ],
)
def test_rejects_query_params_outside_the_allow_list(query):
    with pytest.raises(UnsafeDatabaseURLError):
        assert_local_database_url(f"postgresql://u:p@localhost/db?{query}")


@pytest.mark.parametrize("env_var", ["PGHOSTADDR", "PGSERVICE"])
def test_rejects_libpq_env_vars_that_redirect_the_connection(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "shared")
    with pytest.raises(UnsafeDatabaseURLError):
        assert_local_database_url("postgresql://u:p@localhost/db")


def test_resolves_the_url_from_recording_settings(monkeypatch):
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("DATABASE_CONNECTION_STRING", "postgresql://u:p@localhost:5433/seeded")
    get_settings.cache_clear()
    try:
        assert local_dev_database_url() == "postgresql://u:p@localhost:5433/seeded"
    finally:
        get_settings.cache_clear()


def test_refuses_a_settings_url_that_is_not_local(monkeypatch):
    from transcribe_api.runtime.settings_recording import get_settings

    monkeypatch.setenv("DATABASE_CONNECTION_STRING", "postgresql://u:p@localhost/db?host=shared-db")
    get_settings.cache_clear()
    try:
        with pytest.raises(UnsafeDatabaseURLError):
            local_dev_database_url()
    finally:
        get_settings.cache_clear()

"""Loading CSI-mounted Key Vault files into the environment (runtime/secret_files.py)."""

from __future__ import annotations

import os

from transcribe_api.runtime.secret_files import load_secret_files_into_environ


def test_loads_each_file_as_an_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_AD_TENANT_ID", raising=False)
    monkeypatch.delenv("DATABASE_CONNECTION_STRING", raising=False)
    (tmp_path / "AZURE_AD_TENANT_ID").write_text("tenant-from-vault\n")
    (tmp_path / "DATABASE_CONNECTION_STRING").write_text("postgresql://x")

    loaded = load_secret_files_into_environ(str(tmp_path))

    assert sorted(loaded) == ["AZURE_AD_TENANT_ID", "DATABASE_CONNECTION_STRING"]
    assert os.environ["AZURE_AD_TENANT_ID"] == "tenant-from-vault"  # trailing newline stripped


def test_an_explicit_env_var_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", "explicit")
    (tmp_path / "AZURE_AD_CLIENT_ID").write_text("from-vault")

    assert load_secret_files_into_environ(str(tmp_path)) == []
    assert os.environ["AZURE_AD_CLIENT_ID"] == "explicit"


def test_ignores_csi_bookkeeping_entries(tmp_path):
    (tmp_path / "..data").mkdir()
    (tmp_path / "..2026_10_09").write_text("x")
    assert load_secret_files_into_environ(str(tmp_path)) == []


def test_missing_directory_is_a_no_op(tmp_path):
    assert load_secret_files_into_environ(str(tmp_path / "absent")) == []

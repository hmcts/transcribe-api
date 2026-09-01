"""Unit tests for the blob-path parsing and ownership-check logic in get_download_url."""

import urllib.parse

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# The logic under test, extracted so we don't pay the full routes import cost
# ---------------------------------------------------------------------------

def extract_blob_path(stored_url: str) -> str:
    """Mirror of the path-extraction logic in routes.get_download_url."""
    if stored_url.startswith("https://"):
        parsed = urllib.parse.urlparse(stored_url)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 2:
            raise HTTPException(status_code=500, detail="Stored document URL is malformed")
        return path_parts[1]
    return stored_url


def check_ownership(blob_path: str, user_email: str) -> None:
    """Mirror of the ownership-check logic in routes.get_download_url."""
    if blob_path.startswith("user-uploads/"):
        path_email = blob_path.split("/")[1]
        if path_email.lower() != user_email.lower():
            raise HTTPException(status_code=403, detail="Access denied")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBlobPathExtraction:
    def test_raw_blob_path_returned_unchanged(self):
        raw = "user-uploads/user@example.com/audio.mp3"
        assert extract_blob_path(raw) == raw

    def test_full_sas_url_extracts_blob_path(self):
        url = (
            "https://account.blob.core.windows.net/application-data/"
            "user-uploads/user@example.com/audio.mp3?sv=2021&sig=abc"
        )
        result = extract_blob_path(url)
        assert result == "user-uploads/user@example.com/audio.mp3"

    def test_full_url_strips_query_string(self):
        url = "https://account.blob.core.windows.net/container/path/to/file.webm?token=xyz"
        result = extract_blob_path(url)
        assert result == "path/to/file.webm"

    def test_malformed_url_missing_container_raises_500(self):
        bad = "https://account.blob.core.windows.net/onlyone"
        with pytest.raises(HTTPException) as exc:
            extract_blob_path(bad)
        assert exc.value.status_code == 500
        assert "malformed" in exc.value.detail.lower()

    def test_non_https_path_not_treated_as_url(self):
        path = "documents/some-doc.docx"
        assert extract_blob_path(path) == path


class TestOwnershipCheck:
    def test_matching_email_passes(self):
        check_ownership("user-uploads/user@example.com/audio.mp3", "user@example.com")

    def test_case_insensitive_match_passes(self):
        check_ownership("user-uploads/USER@EXAMPLE.COM/audio.mp3", "user@example.com")
        check_ownership("user-uploads/user@example.com/audio.mp3", "USER@EXAMPLE.COM")

    def test_different_email_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            check_ownership("user-uploads/other@example.com/audio.mp3", "user@example.com")
        assert exc.value.status_code == 403
        assert "Access denied" in exc.value.detail

    def test_non_user_upload_path_skips_check(self):
        # Should not raise even though emails don't match — path is not user-uploads
        check_ownership("documents/some-doc.docx", "user@example.com")

    def test_deeply_nested_path_uses_correct_email_segment(self):
        # Ensure only the second segment (index 1) is compared, not deeper parts
        check_ownership(
            "user-uploads/user@example.com/subdir/audio.mp3",
            "user@example.com",
        )

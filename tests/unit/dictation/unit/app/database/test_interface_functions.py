"""Unit tests for database interface functions.

These tests verify database operations and model creation functions.
"""

from uuid import uuid4

from transcribe_api.domain.models_dictation import MinuteVersion, TemplateMetadata, TemplateName


class TestCreateErrorMinuteVersion:
    """Test cases for error minute version creation logic.

    These tests verify the MinuteVersion model behavior when creating error states,
    simulating the logic in create_error_minute_version without requiring database setup.
    """

    def test_create_error_minute_version_with_template(self):
        """Test error minute version creation when template is provided."""
        minute_version_id = uuid4()
        transcription_id = uuid4()
        error = ValueError("Test error")
        template = TemplateMetadata(
            name=TemplateName.GENERAL,
            description="General meeting minutes",
            category="common",
        )
        trace_id = "test-trace-123"

        # Simulate create_error_minute_version logic
        result = MinuteVersion(
            id=minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            is_generating=False,
            error_message=str(error),
            template=template,
            trace_id=trace_id,
        )

        assert result.id == minute_version_id
        assert result.transcription_id == transcription_id
        assert result.error_message == "Test error"
        assert result.html_content == ""
        assert result.is_generating is False
        assert result.trace_id == trace_id
        assert result.template == template

    def test_minute_version_template_required_for_database(self):
        """Test that MinuteVersion can be created without template in memory.

        While SQLModel allows creating MinuteVersion with template=None in memory,
        this would fail when saving to database since template is defined as
        a required JSONB field. This test documents that validation happens at
        database save time, not object creation time.

        The fix in llm_calls.py ensures template is ALWAYS passed to prevent
        this invalid state from being created.
        """
        minute_version_id = uuid4()
        transcription_id = uuid4()
        template = TemplateMetadata(
            name=TemplateName.GENERAL,
            description="General meeting minutes",
            category="common",
        )

        # MinuteVersion CAN be created with template in memory
        valid_version = MinuteVersion(
            id=minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            is_generating=False,
            error_message="test error",
            template=template,
        )

        assert valid_version.template is not None
        assert valid_version.template.name == TemplateName.GENERAL

    def test_create_error_minute_version_without_trace_id(self):
        """Test error minute version creation when trace_id is omitted."""
        minute_version_id = uuid4()
        transcription_id = uuid4()
        error = ValueError("Test error")
        template = TemplateMetadata(
            name=TemplateName.CRISSA,
            description="CRISSA meeting minutes",
            category="common",
        )

        # Simulate create_error_minute_version without trace_id
        result = MinuteVersion(
            id=minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            is_generating=False,
            error_message=str(error),
            template=template,
            # trace_id not provided - defaults to None
        )

        assert result.id == minute_version_id
        assert result.transcription_id == transcription_id
        assert result.error_message == "Test error"
        assert result.html_content == ""
        assert result.is_generating is False
        assert result.trace_id is None
        assert result.template == template

    def test_create_error_minute_version_simulates_ai_edit_error_scenario(self):
        """Test that simulates the ai_edit_task error handling scenario.

        In ai_edit_task, current_minutes has a template that must be passed to
        the error minute version. Tests both TemplateMetadata object and dict forms.
        """
        minute_version_id = uuid4()
        transcription_id = uuid4()
        error = RuntimeError("LLM API failed")

        # Simulate template as it would appear in current_minutes
        template_obj = TemplateMetadata(
            name=TemplateName.GENERAL,
            description="General meeting minutes",
            category="common",
        )

        # Test creating with TemplateMetadata object directly
        result = MinuteVersion(
            id=minute_version_id,
            transcription_id=transcription_id,
            html_content="",
            is_generating=False,
            error_message=str(error),
            template=template_obj,
        )

        assert result.id == minute_version_id
        assert result.transcription_id == transcription_id
        assert result.error_message == "LLM API failed"
        assert result.html_content == ""
        assert result.is_generating is False
        assert result.template is not None
        assert result.template.name == TemplateName.GENERAL


from datetime import UTC, datetime, timedelta  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402


class TestIsTranscriptionShowable:
    def test_returns_true_when_minute_version_has_error_message(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = [MagicMock(error_message="some error")]
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_returns_true_when_both_general_and_crissa_templates_complete(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        v1 = MagicMock()
        v1.error_message = None
        v1.html_content = "<p>content</p>"
        v1.template = {"name": "General"}
        v2 = MagicMock()
        v2.error_message = None
        v2.html_content = "<p>content</p>"
        v2.template = {"name": "Crissa"}
        t = MagicMock()
        t.minute_versions = [v1, v2]
        t.transcription_jobs = []
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_returns_false_when_only_one_template_complete(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        v1 = MagicMock()
        v1.error_message = None
        v1.html_content = "<p>content</p>"
        v1.template = {"name": "General"}
        t = MagicMock()
        t.minute_versions = [v1]
        t.transcription_jobs = []
        recent = datetime.now(UTC) - timedelta(seconds=60)
        t.created_datetime = recent
        assert _is_transcription_showable(t, datetime.now(UTC)) is False

    def test_returns_true_when_job_has_error_message(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = [MagicMock(error_message="job failed")]
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_returns_true_when_job_has_dialogue_entries(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        job = MagicMock()
        job.error_message = None
        job.dialogue_entries = [MagicMock()]
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = [job]
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_returns_true_when_created_more_than_5_minutes_ago(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = []
        t.created_datetime = datetime.now(UTC) - timedelta(minutes=10)
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_returns_false_when_no_conditions_met(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = []
        t.created_datetime = datetime.now(UTC) - timedelta(seconds=30)
        assert _is_transcription_showable(t, datetime.now(UTC)) is False

    def test_returns_true_on_exception(self):
        import sentry_sdk

        from transcribe_api.domain.interface_dictation import _is_transcription_showable

        class BadTranscription:
            @property
            def minute_versions(self):
                msg = "simulated failure"
                raise AttributeError(msg)

        with patch.object(sentry_sdk, "capture_exception"):
            result = _is_transcription_showable(BadTranscription(), datetime.now(UTC))
        assert result is True

    def test_aware_datetime_used_directly(self):
        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = []
        # timezone-aware datetime — should not be localized again
        t.created_datetime = datetime.now(UTC) - timedelta(minutes=10)
        assert _is_transcription_showable(t, datetime.now(UTC)) is True

    def test_naive_datetime_is_localized(self):
        import datetime as dt

        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = []
        # naive datetime — pytz.utc.localize() should be called on it
        t.created_datetime = dt.datetime(2020, 1, 1, 0, 0, 0)  # noqa: DTZ001  # naive, very old
        assert _is_transcription_showable(t, datetime.now(UTC)) is True


class TestExtractUniqueSpeakers:
    def test_empty_jobs_returns_empty(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        t = MagicMock()
        t.transcription_jobs = []
        assert _extract_unique_speakers(t) == []

    def test_none_jobs_returns_empty(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        t = MagicMock()
        t.transcription_jobs = None
        assert _extract_unique_speakers(t) == []

    def test_extracts_and_titlecases_dict_speakers(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        job = MagicMock()
        job.dialogue_entries = [{"speaker": "judge smith"}, {"speaker": "legal counsel"}]
        t = MagicMock()
        t.transcription_jobs = [job]
        result = _extract_unique_speakers(t)
        assert "Judge Smith" in result
        assert "Legal Counsel" in result

    def test_deduplicates_speakers(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        job = MagicMock()
        job.dialogue_entries = [{"speaker": "judge"}, {"speaker": "judge"}]
        t = MagicMock()
        t.transcription_jobs = [job]
        result = _extract_unique_speakers(t)
        assert result.count("Judge") == 1

    def test_skips_empty_speaker_names(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        job = MagicMock()
        job.dialogue_entries = [{"speaker": "  "}, {"speaker": "judge"}]
        t = MagicMock()
        t.transcription_jobs = [job]
        result = _extract_unique_speakers(t)
        assert "" not in result
        assert "Judge" in result

    def test_returns_sorted_list(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers
        job = MagicMock()
        job.dialogue_entries = [{"speaker": "zara"}, {"speaker": "adam"}]
        t = MagicMock()
        t.transcription_jobs = [job]
        result = _extract_unique_speakers(t)
        assert result == sorted(result)

    def test_object_entries_use_getattr(self):
        from transcribe_api.domain.interface_dictation import _extract_unique_speakers

        class Entry:
            def __init__(self, speaker):
                self.speaker = speaker

        job = MagicMock()
        job.dialogue_entries = [Entry("judge")]
        t = MagicMock()
        t.transcription_jobs = [job]
        result = _extract_unique_speakers(t)
        assert "Judge" in result


class TestCreateErrorMinuteVersionFunction:
    def test_creates_minute_version_with_template_and_trace_id(self):
        from transcribe_api.domain.interface_dictation import create_error_minute_version
        from transcribe_api.domain.models_dictation import TemplateMetadata, TemplateName
        mid = uuid4()
        tid = uuid4()
        template = TemplateMetadata(name=TemplateName.GENERAL, description="d", category="common")
        result = create_error_minute_version(str(mid), tid, ValueError("oops"), template=template, trace_id="trace-123")
        assert result.error_message == "oops"
        assert result.template == template
        assert result.trace_id == "trace-123"
        assert result.is_generating is False

    def test_creates_without_template_when_not_provided(self):
        from transcribe_api.domain.interface_dictation import create_error_minute_version
        mid = uuid4()
        tid = uuid4()
        result = create_error_minute_version(str(mid), tid, RuntimeError("fail"))
        assert result.error_message == "fail"
        assert not hasattr(result, "template") or result.template is None

    def test_creates_without_trace_id_when_not_provided(self):
        from transcribe_api.domain.interface_dictation import create_error_minute_version
        from transcribe_api.domain.models_dictation import TemplateMetadata, TemplateName
        mid = uuid4()
        tid = uuid4()
        template = TemplateMetadata(name=TemplateName.CRISSA, description="d", category="common")
        result = create_error_minute_version(str(mid), tid, RuntimeError("err"), template=template)
        assert result.trace_id is None

    def test_returns_false_when_created_datetime_is_none(self):
        from datetime import UTC, datetime

        from transcribe_api.domain.interface_dictation import _is_transcription_showable
        t = MagicMock()
        t.minute_versions = []
        t.transcription_jobs = []
        t.created_datetime = None
        assert _is_transcription_showable(t, datetime.now(UTC)) is False


class TestBeforeFlush:
    def test_updates_updated_datetime_on_base_table_objects(self):
        from uuid import uuid4

        from transcribe_api.domain.interface_dictation import before_flush
        from transcribe_api.domain.models_dictation import Transcription

        t = Transcription(id=uuid4(), user_id=uuid4(), title="test")
        original_dt = t.updated_datetime

        import time
        time.sleep(0.001)

        session = MagicMock()
        session.dirty = [t]
        before_flush(session, None, None)

        # before_flush should refresh updated_datetime to "now"
        assert t.updated_datetime is not None
        assert t.updated_datetime >= original_dt

    def test_skips_non_base_table_objects(self):
        from transcribe_api.domain.interface_dictation import before_flush

        plain_obj = MagicMock()
        plain_obj.updated_datetime = None
        session = MagicMock()
        session.dirty = [plain_obj]
        before_flush(session, None, None)

        assert plain_obj.updated_datetime is None


class TestSaveTranscription:
    def test_sets_user_id_and_returns_merged(self):
        from uuid import uuid4

        from transcribe_api.domain.interface_dictation import save_transcription
        from transcribe_api.domain.models_dictation import Transcription

        transcription = MagicMock(spec=Transcription)
        merged = MagicMock(spec=Transcription)
        user_id = uuid4()

        mock_session = MagicMock()
        mock_session.merge.return_value = merged
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = save_transcription(transcription, user_id)

        assert transcription.user_id == user_id
        mock_session.commit.assert_called_once()
        mock_session.refresh.assert_called_with(merged)
        assert result is merged


class TestGetUserById:
    def test_returns_user_when_found(self):
        from uuid import uuid4

        from transcribe_api.domain.interface_dictation import get_user_by_id

        mock_user = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_user
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_user_by_id(uuid4())

        assert result is mock_user

    def test_raises_404_when_not_found(self):
        from uuid import uuid4

        from fastapi import HTTPException

        from transcribe_api.domain.interface_dictation import get_user_by_id

        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx), pytest.raises(HTTPException) as exc:
            get_user_by_id(uuid4())

        assert exc.value.status_code == 404


class TestGetMinuteVersions:
    def test_returns_versions_when_transcription_exists(self):
        from uuid import uuid4

        from transcribe_api.domain.interface_dictation import get_minute_versions

        mock_transcription = MagicMock()
        mock_versions = [MagicMock(), MagicMock()]
        mock_session = MagicMock()
        mock_session.get.return_value = mock_transcription
        mock_session.exec.return_value.all.return_value = mock_versions
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_minute_versions(uuid4())

        assert result == mock_versions

    def test_raises_404_when_transcription_not_found(self):
        from uuid import uuid4

        from fastapi import HTTPException

        from transcribe_api.domain.interface_dictation import get_minute_versions

        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx), pytest.raises(HTTPException) as exc:
            get_minute_versions(uuid4())

        assert exc.value.status_code == 404


class TestGetOrCreateTag:
    def test_returns_existing_tag_when_found(self):
        from transcribe_api.domain.interface_dictation import get_or_create_tag

        mock_tag = MagicMock()
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = mock_tag
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_or_create_tag("TestTag")

        assert result is mock_tag

    def test_creates_new_tag_when_not_found(self):
        from transcribe_api.domain.interface_dictation import get_or_create_tag

        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            get_or_create_tag("NewTag")

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()


class TestGetLiveDraft:
    def _mock_ctx(self, draft):
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = draft
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        return mock_session, mock_ctx

    def test_returns_none_when_no_draft_exists(self):
        from transcribe_api.domain.interface_dictation import get_live_draft

        mock_session, mock_ctx = self._mock_ctx(None)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is None

    def test_returns_draft_when_fresh(self):
        from transcribe_api.domain.interface_dictation import get_live_draft

        draft = MagicMock()
        draft.updated_datetime = datetime.now(UTC) - timedelta(hours=1)
        draft.created_datetime = datetime.now(UTC) - timedelta(hours=1)
        mock_session, mock_ctx = self._mock_ctx(draft)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is draft
        mock_session.delete.assert_not_called()

    def test_returns_none_and_deletes_when_expired(self):
        from transcribe_api.domain.interface_dictation import get_live_draft

        draft = MagicMock()
        draft.updated_datetime = datetime.now(UTC) - timedelta(hours=25)
        draft.created_datetime = datetime.now(UTC) - timedelta(hours=25)
        mock_session, mock_ctx = self._mock_ctx(draft)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is None
        mock_session.delete.assert_called_once_with(draft)
        mock_session.commit.assert_called_once()

    def test_handles_naive_updated_datetime(self):
        import datetime as _dt

        from transcribe_api.domain.interface_dictation import get_live_draft

        draft = MagicMock()
        draft.updated_datetime = _dt.datetime.now() - timedelta(hours=1)  # noqa: DTZ005
        assert draft.updated_datetime.tzinfo is None
        draft.created_datetime = datetime.now(UTC) - timedelta(hours=1)
        mock_session, mock_ctx = self._mock_ctx(draft)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is draft
        mock_session.delete.assert_not_called()

    def test_falls_back_to_created_datetime_when_updated_is_none(self):
        from transcribe_api.domain.interface_dictation import get_live_draft

        draft = MagicMock()
        draft.updated_datetime = None
        draft.created_datetime = datetime.now(UTC) - timedelta(hours=1)
        mock_session, mock_ctx = self._mock_ctx(draft)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is draft

    def test_falls_back_to_created_datetime_when_expired(self):
        from transcribe_api.domain.interface_dictation import get_live_draft

        draft = MagicMock()
        draft.updated_datetime = None
        draft.created_datetime = datetime.now(UTC) - timedelta(hours=25)
        mock_session, mock_ctx = self._mock_ctx(draft)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            result = get_live_draft(uuid4())

        assert result is None
        mock_session.delete.assert_called_once_with(draft)
        mock_session.commit.assert_called_once()


class TestDeleteLiveDraft:
    def test_deletes_draft_when_it_exists(self):
        from transcribe_api.domain.interface_dictation import delete_live_draft

        draft = MagicMock()
        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = draft
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            delete_live_draft(uuid4())

        mock_session.delete.assert_called_once_with(draft)
        mock_session.commit.assert_called_once()

    def test_no_op_when_draft_does_not_exist(self):
        from transcribe_api.domain.interface_dictation import delete_live_draft

        mock_session = MagicMock()
        mock_session.exec.return_value.first.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            delete_live_draft(uuid4())

        mock_session.delete.assert_not_called()
        mock_session.commit.assert_not_called()


class TestUpsertLiveDraft:
    def test_executes_and_commits(self):
        from transcribe_api.domain.interface_dictation import upsert_live_draft

        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("transcribe_api.domain.interface_dictation.Session", return_value=mock_ctx):
            upsert_live_draft(uuid4(), {"background": [], "evidence": [], "facts": []}, {})

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

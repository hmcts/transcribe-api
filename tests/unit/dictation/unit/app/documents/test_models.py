"""Unit tests for document generation models."""


from transcribe_api.documents.models import (
    DocumentData,
    HearingFormData,
    SectionTranscripts,
    TranscriptMessage,
    format_date_long,
)


class TestFormatDateLong:
    def test_standard_date(self):
        assert format_date_long("2026-01-12") == "12 January 2026"

    def test_single_digit_day(self):
        assert format_date_long("2025-03-05") == "5 March 2025"

    def test_december(self):
        assert format_date_long("2024-12-31") == "31 December 2024"

    def test_empty_string_returns_empty(self):
        assert format_date_long("") == ""

    def test_invalid_format_returns_original(self):
        assert format_date_long("not-a-date") == "not-a-date"

    def test_partial_date_returns_original(self):
        assert format_date_long("2026-01") == "2026-01"


def _make_form(**kwargs) -> HearingFormData:
    defaults = {
        "location": "Manchester",
        "hearing_date": "2026-01-15",
        "judge_name": "Judge Smith",
        "appellant_name": "Mr Jones",
        "respondent": "Secretary of State",
        "hearing_type": "face_to_face",
        "appealable_decision_date": "2025-06-01",
        "document_type": "hearing",
    }
    defaults.update(kwargs)
    return HearingFormData(**defaults)


class TestHearingFormDataGetAppellantRepValue:
    def test_no_rep_no_attend(self):
        form = _make_form(appellant_rep_type="No representative and did not attend")
        assert form.get_appellant_rep_value() == "No representative and did not attend"

    def test_self_rep(self):
        form = _make_form(appellant_rep_type="Representing themselves")
        assert form.get_appellant_rep_value() == "Representing him or herself"

    def test_represented_returns_details(self):
        form = _make_form(appellant_rep_type="Represented", appellant_rep_details="Ms Brown, Counsel")
        assert form.get_appellant_rep_value() == "Ms Brown, Counsel"

    def test_represented_no_details_returns_empty(self):
        form = _make_form(appellant_rep_type="Represented", appellant_rep_details=None)
        assert form.get_appellant_rep_value() == ""

    def test_unknown_type_falls_back_to_details(self):
        form = _make_form(appellant_rep_type="Something new", appellant_rep_details="Fallback")
        assert form.get_appellant_rep_value() == "Fallback"

    def test_no_rep_type_falls_back_to_details(self):
        form = _make_form(appellant_rep_type=None, appellant_rep_details="Old field value")
        assert form.get_appellant_rep_value() == "Old field value"

    def test_no_rep_type_no_details_returns_empty(self):
        form = _make_form(appellant_rep_type=None, appellant_rep_details=None)
        assert form.get_appellant_rep_value() == ""


class TestHearingFormDataRequiresAppellantRule28:
    def test_no_rep_no_attend_requires_rule_28(self):
        form = _make_form(appellant_rep_type="No representative and did not attend")
        assert form.requires_appellant_rule_28_text() is True

    def test_self_rep_does_not_require_rule_28(self):
        form = _make_form(appellant_rep_type="Representing themselves")
        assert form.requires_appellant_rule_28_text() is False

    def test_represented_does_not_require_rule_28(self):
        form = _make_form(appellant_rep_type="Represented")
        assert form.requires_appellant_rule_28_text() is False

    def test_none_rep_type_does_not_require_rule_28(self):
        form = _make_form(appellant_rep_type=None)
        assert form.requires_appellant_rule_28_text() is False


class TestHearingFormDataGetRespondentRepValue:
    def test_no_rep(self):
        form = _make_form(respondent_rep_type="No representative")
        assert form.get_respondent_rep_value() == "No Home Office Presenting Officer"

    def test_hopo_with_name(self):
        form = _make_form(respondent_rep_type="Home Office Presenting Officer", respondent_rep_name="Mr A")
        assert form.get_respondent_rep_value() == "Mr A, Home Office Presenting Officer"

    def test_hopo_without_name(self):
        form = _make_form(respondent_rep_type="Home Office Presenting Officer", respondent_rep_name=None)
        assert form.get_respondent_rep_value() == "Home Office Presenting Officer"

    def test_counsel_with_name(self):
        form = _make_form(respondent_rep_type="Counsel", respondent_rep_name="Ms B")
        assert form.get_respondent_rep_value() == "Ms B, Counsel"

    def test_counsel_without_name(self):
        form = _make_form(respondent_rep_type="Counsel", respondent_rep_name=None)
        assert form.get_respondent_rep_value() == "Counsel"

    def test_unknown_type_falls_back_to_name(self):
        form = _make_form(respondent_rep_type="Unknown", respondent_rep_name="Some name")
        assert form.get_respondent_rep_value() == "Some name"

    def test_none_type_falls_back_to_name(self):
        form = _make_form(respondent_rep_type=None, respondent_rep_name="Fallback")
        assert form.get_respondent_rep_value() == "Fallback"


class TestHearingFormDataRequiresRespondentRule28:
    def test_no_rep_requires_rule_28(self):
        form = _make_form(respondent_rep_type="No representative")
        assert form.requires_respondent_rule_28_text() is True

    def test_hopo_does_not_require_rule_28(self):
        form = _make_form(respondent_rep_type="Home Office Presenting Officer")
        assert form.requires_respondent_rule_28_text() is False

    def test_none_does_not_require_rule_28(self):
        form = _make_form(respondent_rep_type=None)
        assert form.requires_respondent_rule_28_text() is False


class TestHearingFormDataToTemplateDict:
    def test_returns_expected_keys(self):
        form = _make_form()
        d = form.to_template_dict()
        expected = {
            "CaseID", "Location", "Jurisdiction", "Hearingdate", "Judge", "EndJudge",
            "Appellant", "Respondent", "HearingDescription", "AppRep", "RespRep",
            "ForTheAppellant", "ForTheRespondent", "AppealableDecDate", "Issues", "LegalFramework",
        }
        assert expected.issubset(d.keys())

    def test_hearing_date_formatted(self):
        form = _make_form(hearing_date="2026-03-07")
        assert form.to_template_dict()["Hearingdate"] == "7 March 2026"

    def test_case_id_empty_when_none(self):
        form = _make_form()
        assert form.to_template_dict()["CaseID"] == ""

    def test_case_id_populated(self):
        form = _make_form(case_id="IA/12345/2025")
        assert form.to_template_dict()["CaseID"] == "IA/12345/2025"

    def test_location_other_overrides_when_other(self):
        form = _make_form(location="Other", location_other="Remote")
        assert form.to_template_dict()["Location"] == "Remote"

    def test_location_used_when_not_other(self):
        form = _make_form(location="Birmingham")
        assert form.to_template_dict()["Location"] == "Birmingham"

    def test_for_appellant_label_set_when_rep_value_present(self):
        form = _make_form(appellant_rep_type="Representing themselves")
        d = form.to_template_dict()
        assert d["ForTheAppellant"] == "For the Appellant:"

    def test_for_appellant_label_empty_when_no_rep_value(self):
        form = _make_form(appellant_rep_type=None, appellant_rep_details=None)
        d = form.to_template_dict()
        assert d["ForTheAppellant"] == ""

    def test_legal_issues_joined(self):
        form = _make_form(legal_issues=["asylum_pre_naba", "eea"])
        d = form.to_template_dict()
        assert d["Issues"] == "asylum_pre_naba, eea"
        assert d["LegalFramework"] == "asylum_pre_naba, eea"

    def test_no_legal_issues_empty_string(self):
        form = _make_form(legal_issues=[])
        d = form.to_template_dict()
        assert d["Issues"] == ""


class TestSectionTranscripts:
    def _msg(self, text: str, speaker: str = "Speaker 0") -> TranscriptMessage:
        return TranscriptMessage(speaker=speaker, text=text, timestamp="00:01:00")

    def test_format_section_empty_returns_empty_string(self):
        st = SectionTranscripts()
        assert st.format_section("background") == ""

    def test_format_section_single_message(self):
        st = SectionTranscripts(background=[self._msg("Hello")])
        assert st.format_section("background") == "Hello"

    def test_format_section_multiple_messages_joined_with_double_newline(self):
        st = SectionTranscripts(background=[self._msg("A"), self._msg("B")])
        assert st.format_section("background") == "A\n\nB"

    def test_format_section_evidence(self):
        st = SectionTranscripts(evidence=[self._msg("Evidence text")])
        assert st.format_section("evidence") == "Evidence text"

    def test_format_section_facts(self):
        st = SectionTranscripts(facts=[self._msg("Fact text")])
        assert st.format_section("facts") == "Fact text"

    def test_get_all_messages_combines_sections(self):
        st = SectionTranscripts(
            background=[self._msg("B1"), self._msg("B2")],
            evidence=[self._msg("E1")],
            facts=[self._msg("F1"), self._msg("F2")],
        )
        all_msgs = st.get_all_messages()
        assert len(all_msgs) == 5
        texts = [m.text for m in all_msgs]
        assert texts == ["B1", "B2", "E1", "F1", "F2"]

    def test_get_all_messages_empty(self):
        assert SectionTranscripts().get_all_messages() == []


class TestDocumentData:
    def _form(self) -> HearingFormData:
        return _make_form()

    def _msg(self, text: str) -> TranscriptMessage:
        return TranscriptMessage(speaker="Speaker 0", text=text, timestamp="00:00:01")

    def test_get_section_transcripts_passes_through_section_transcripts(self):
        st = SectionTranscripts(background=[self._msg("Hello")])
        doc = DocumentData(form_data=self._form(), messages=st, user_email="a@b.com")
        assert doc.get_section_transcripts() is st

    def test_get_section_transcripts_wraps_list_in_background(self):
        msgs = [self._msg("One"), self._msg("Two")]
        doc = DocumentData(form_data=self._form(), messages=msgs, user_email="a@b.com")
        st = doc.get_section_transcripts()
        assert isinstance(st, SectionTranscripts)
        assert len(st.background) == 2
        assert st.evidence == []

    def test_format_transcript_no_messages(self):
        doc = DocumentData(form_data=self._form(), messages=[], user_email="a@b.com")
        assert doc.format_transcript() == "No transcript available."

    def test_format_transcript_multiple_messages(self):
        msgs = [self._msg("First"), self._msg("Second")]
        doc = DocumentData(form_data=self._form(), messages=msgs, user_email="a@b.com")
        assert doc.format_transcript() == "First\n\nSecond"

    def test_to_template_dict_includes_transcript(self):
        doc = DocumentData(form_data=self._form(), messages=[], user_email="a@b.com")
        d = doc.to_template_dict()
        assert "Transcript" in d


class TestTranscriptMessageFormatForDocument:
    def test_format_includes_speaker_timestamp_and_text(self):
        msg = TranscriptMessage(speaker="Speaker 1", text="Hello there", timestamp="00:02:30")
        result = msg.format_for_document()
        assert result == "Speaker 1 [00:02:30]: Hello there"

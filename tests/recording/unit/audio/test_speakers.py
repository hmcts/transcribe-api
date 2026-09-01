"""Unit tests for speaker grouping/normalisation/labelling.

Regression coverage: these transforms reconstruct DialogueEntry objects and
previously dropped confidence/words entirely, silently breaking word-level
confidence highlighting for every real transcription (only ever caught by
manually seeding a database with synthetic data, which bypassed this code
path).
"""

from transcribe_api.stt.speakers_recording import (
    add_speaker_labels,
    group_dialogue_entries_by_speaker,
    normalize_speaker_labels,
    process_speakers,
)
from transcribe_api.domain.models_recording import (
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    WordInfo,
)


def _entry(
    speaker: str,
    text: str,
    confidence: float | None = None,
    words: list[WordInfo] | None = None,
    alternatives: list[PhraseAlternatives] | None = None,
    start_time: float = 0.0,
    end_time: float = 1.0,
) -> DialogueEntry:
    return DialogueEntry(
        speaker=speaker,
        text=text,
        start_time=start_time,
        end_time=end_time,
        confidence=confidence,
        words=words,
        alternatives=alternatives,
    )


def _alternatives(
    *texts: str, start_word_index: int | None = None, end_word_index: int | None = None
) -> list[PhraseAlternatives]:
    return [
        PhraseAlternatives(
            start_word_index=start_word_index,
            end_word_index=end_word_index,
            candidates=[NBestCandidate(text=t) for t in texts],
        )
    ]


def _words(*texts: str) -> list[WordInfo]:
    return [
        WordInfo(text=t, start_time=float(i), end_time=float(i) + 1, confidence=0.9)
        for i, t in enumerate(texts)
    ]


class TestGroupDialogueEntriesBySpeaker:
    def test_preserves_confidence_and_words_for_a_single_entry(self):
        entries = [_entry("0", "hello", confidence=0.8, words=_words("hello"))]
        result = group_dialogue_entries_by_speaker(entries)
        assert result[0].confidence == 0.8
        assert result[0].words == _words("hello")

    def test_concatenates_words_across_merged_entries(self):
        entries = [
            _entry("0", "the quick", confidence=0.9, words=_words("the", "quick")),
            _entry("0", "brown fox", confidence=0.9, words=_words("brown", "fox")),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert [w.text for w in result[0].words] == ["the", "quick", "brown", "fox"]

    def test_weights_merged_confidence_by_word_count(self):
        entries = [
            _entry("0", "one two", confidence=1.0),  # 2 words
            _entry("0", "three", confidence=0.0),  # 1 word
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        # (1.0*2 + 0.0*1) / 3 = 0.666...
        assert abs(result[0].confidence - (2 / 3)) < 1e-9

    def test_unscored_entry_does_not_drag_down_a_scored_merge(self):
        # A merged-in phrase with no confidence at all must not count as a
        # 0.0-confidence phrase — it should simply not contribute to the
        # weighting, leaving the scored phrase's own confidence intact.
        entries = [
            _entry("0", "one two", confidence=0.8),  # 2 words, scored
            _entry("0", "three four", confidence=None),  # 2 words, unscored
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].confidence == 0.8

    def test_does_not_merge_across_different_speakers(self):
        entries = [
            _entry("0", "hello", confidence=0.9, words=_words("hello")),
            _entry("1", "hi there", confidence=0.8, words=_words("hi", "there")),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2
        assert result[0].confidence == 0.9
        assert result[1].confidence == 0.8

    def test_handles_missing_confidence_and_words_gracefully(self):
        entries = [_entry("0", "hello"), _entry("0", "world")]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].confidence is None
        assert result[0].words is None

    def test_preserves_alternatives_for_a_single_entry(self):
        entries = [
            _entry(
                "0",
                "hello",
                confidence=0.8,
                words=_words("hello"),
                alternatives=_alternatives("hello", "yellow", start_word_index=0, end_word_index=0),
            )
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert result[0].alternatives is not None
        assert [c.text for c in result[0].alternatives[0].candidates] == ["hello", "yellow"]
        assert result[0].alternatives[0].start_word_index == 0
        assert result[0].alternatives[0].end_word_index == 0

    def test_offsets_alternatives_word_indices_when_merging_aligned_entries(self):
        entries = [
            _entry(
                "0",
                "the quick",
                confidence=0.9,
                words=_words("the", "quick"),
                alternatives=_alternatives("the quick", start_word_index=0, end_word_index=1),
            ),
            _entry(
                "0",
                "brown fox",
                confidence=0.9,
                words=_words("brown", "fox"),
                alternatives=_alternatives(
                    "brown fox", "brown fax", start_word_index=0, end_word_index=1
                ),
            ),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert len(result[0].alternatives) == 2
        first, second = result[0].alternatives
        assert (first.start_word_index, first.end_word_index) == (0, 1)
        # Offset by 2 (the first phrase's word count) so indices still point
        # into the merged 4-word list ("the", "quick", "brown", "fox").
        assert (second.start_word_index, second.end_word_index) == (2, 3)
        assert [c.text for c in second.candidates] == ["brown fox", "brown fax"]

    def test_alternatives_degrade_to_unindexed_when_words_alignment_breaks(self):
        # Mirrors the words-merge rule: when only one side has word-level
        # detail, the merged entry's words become None. The alternatives
        # must not be silently dropped in that case — they're kept, just
        # without a word-index range that no longer has anything to point at.
        entries = [
            _entry(
                "0",
                "the quick",
                confidence=0.9,
                words=_words("the", "quick"),
                alternatives=_alternatives("the quick", start_word_index=0, end_word_index=1),
            ),
            _entry(
                "0",
                "brown fox",
                confidence=0.9,
                words=None,
                alternatives=_alternatives("brown fox"),
            ),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].words is None
        assert len(result[0].alternatives) == 2
        for group in result[0].alternatives:
            assert group.start_word_index is None
            assert group.end_word_index is None
        assert {c.text for group in result[0].alternatives for c in group.candidates} == {
            "the quick",
            "brown fox",
        }

    def test_handles_missing_alternatives_gracefully(self):
        entries = [_entry("0", "hello"), _entry("0", "world")]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].alternatives is None

    def test_drops_words_entirely_when_only_one_side_has_them(self):
        # A partial words list (covering only one side of the merge) would
        # no longer line up with the merged text's word indices, which
        # would corrupt word-range corrections and playback-sync
        # highlighting — so the merged entry must not have words at all
        # rather than a misleading partial list.
        entries = [
            _entry("0", "the quick", confidence=0.9, words=_words("the", "quick")),
            _entry("0", "brown fox", confidence=0.9, words=None),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].words is None


class TestMidSentenceMisattribution:
    """DIAAT-237: Azure occasionally flips the raw speaker id for the tail
    end of a sentence — often just the last few words — even though the
    same person kept speaking right through. These fixtures reconstruct
    that pattern (near-zero gap, no sentence-ending punctuation yet, a
    short trailing fragment) and check it gets reattributed to the speaker
    who said the rest of the sentence, alongside guards that stop the same
    heuristic from swallowing genuinely different speakers.
    """

    def test_merges_a_clipped_sentence_tail_back_into_the_original_speaker(self):
        # "I think we should go to the shop now." split across a phrase
        # boundary, with the last two words mis-diarised as speaker "1".
        entries = [
            _entry(
                "0",
                "I think we should go to the",
                confidence=0.9,
                start_time=0.0,
                end_time=2.0,
            ),
            _entry(
                "1",
                "shop now.",
                confidence=0.8,
                start_time=2.05,  # 50ms gap — continuous speech, not a pause
                end_time=2.6,
            ),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].speaker == "0"
        assert result[0].text == "I think we should go to the shop now."

    def test_full_pipeline_reattributes_clipped_tail_to_a_single_speaker_label(self):
        entries = [
            _entry(
                "0",
                "I think we should go to the",
                confidence=0.9,
                start_time=0.0,
                end_time=2.0,
            ),
            _entry("1", "shop now.", confidence=0.8, start_time=2.05, end_time=2.6),
            # Genuine continuation from the same speaker after a real pause.
            _entry("0", "And get some milk.", confidence=0.9, start_time=4.0, end_time=5.5),
        ]
        result = process_speakers(entries)
        assert len(result) == 1
        assert result[0].speaker == "Speaker 0"
        assert result[0].text == "I think we should go to the shop now. And get some milk."

    def test_does_not_merge_when_the_speaker_change_follows_a_real_pause(self):
        entries = [
            _entry("0", "I think we should go", confidence=0.9, start_time=0.0, end_time=2.0),
            _entry("1", "Sounds good to me.", confidence=0.9, start_time=3.5, end_time=4.8),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2
        assert result[0].speaker == "0"
        assert result[1].speaker == "1"

    def test_does_not_merge_across_a_sentence_boundary_even_with_a_tiny_gap(self):
        # The first speaker actually finished their sentence — a quick
        # back-and-forth exchange right after shouldn't be treated as a
        # mid-sentence artifact just because the gap is small.
        entries = [
            _entry("0", "Are you ready?", confidence=0.9, start_time=0.0, end_time=1.0),
            _entry("1", "Yes, let's go.", confidence=0.9, start_time=1.05, end_time=2.0),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2
        assert result[0].speaker == "0"
        assert result[1].speaker == "1"

    def test_does_not_merge_a_long_fragment_even_with_a_tiny_gap(self):
        # A substantial run of new text is far more likely to be a genuine
        # (if fast) speaker handover than a clipped one- or two-word tail.
        entries = [
            _entry(
                "0", "I think we should go to the", confidence=0.9, start_time=0.0, end_time=2.0
            ),
            _entry(
                "1",
                "shop and then head over to the market before it closes",
                confidence=0.9,
                start_time=2.05,
                end_time=4.5,
            ),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2
        assert result[0].speaker == "0"
        assert result[1].speaker == "1"

    def test_does_not_merge_a_large_timestamp_overlap(self):
        # A gap far more negative than plausible timestamp jitter is
        # crosstalk between two real speakers, not the same phrase.
        entries = [
            _entry(
                "0", "I think we should go to the", confidence=0.9, start_time=0.0, end_time=2.0
            ),
            _entry("1", "shop now.", confidence=0.8, start_time=0.5, end_time=1.0),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2

    def test_merge_never_shrinks_the_segment_end_time(self):
        # The small negative gap tolerated for jitter means a merged-in
        # fragment can end slightly before the current segment already
        # does — the merge must not let that pull end_time backwards.
        entries = [
            _entry(
                "0", "I think we should go to the", confidence=0.9, start_time=0.0, end_time=2.5
            ),
            _entry(
                "1",
                "shop now.",
                confidence=0.8,
                start_time=2.4,  # overlaps, but within the tolerated jitter window
                end_time=2.45,  # ends *before* the current segment's end_time
            ),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].end_time == 2.5


class TestNormalizeSpeakerLabels:
    def test_preserves_confidence_and_words(self):
        entries = [_entry("2", "hello", confidence=0.75, words=_words("hello"))]
        result = normalize_speaker_labels(entries)
        assert result[0].confidence == 0.75
        assert result[0].words == _words("hello")

    def test_preserves_alternatives(self):
        entries = [_entry("2", "hello", alternatives=_alternatives("hello", "yellow"))]
        result = normalize_speaker_labels(entries)
        assert [c.text for c in result[0].alternatives[0].candidates] == ["hello", "yellow"]

    def test_relabels_speakers_to_sequential_ids(self):
        entries = [_entry("guid-b", "hi"), _entry("guid-a", "there")]
        result = normalize_speaker_labels(entries)
        assert result[0].speaker == "0"
        assert result[1].speaker == "1"


class TestAddSpeakerLabels:
    def test_preserves_confidence_and_words(self):
        entries = [_entry("0", "hello", confidence=0.65, words=_words("hello"))]
        result = add_speaker_labels(entries)
        assert result[0].confidence == 0.65
        assert result[0].words == _words("hello")

    def test_preserves_alternatives(self):
        entries = [_entry("0", "hello", alternatives=_alternatives("hello", "yellow"))]
        result = add_speaker_labels(entries)
        assert [c.text for c in result[0].alternatives[0].candidates] == ["hello", "yellow"]

    def test_prefixes_speaker_label(self):
        entries = [_entry("0", "hello")]
        result = add_speaker_labels(entries)
        assert result[0].speaker == "Speaker 0"


class TestProcessSpeakers:
    def test_confidence_and_words_survive_the_full_pipeline(self):
        entries = [
            _entry("0", "the quick", confidence=0.9, words=_words("the", "quick")),
            _entry("0", "brown fox", confidence=0.7, words=_words("brown", "fox")),
        ]
        result = process_speakers(entries)
        assert len(result) == 1
        assert result[0].speaker == "Speaker 0"
        assert result[0].confidence is not None
        assert [w.text for w in result[0].words] == ["the", "quick", "brown", "fox"]

    def test_alternatives_survive_the_full_pipeline(self):
        entries = [
            _entry(
                "0",
                "the quick",
                confidence=0.9,
                words=_words("the", "quick"),
                alternatives=_alternatives("the quick", start_word_index=0, end_word_index=1),
            ),
            _entry(
                "0",
                "brown fox",
                confidence=0.7,
                words=_words("brown", "fox"),
                alternatives=_alternatives(
                    "brown fox", "brown fax", start_word_index=0, end_word_index=1
                ),
            ),
        ]
        result = process_speakers(entries)
        assert len(result) == 1
        assert len(result[0].alternatives) == 2
        assert (
            result[0].alternatives[1].start_word_index,
            result[0].alternatives[1].end_word_index,
        ) == (
            2,
            3,
        )

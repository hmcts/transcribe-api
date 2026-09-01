"""Word error rate: only meaningful once a human has corrected a segment.

Azure's own per-phrase confidence score is not an accuracy measurement —
there's nothing to compare it against. A real WER needs a reference
transcript, which only exists once a clerk has corrected the
auto-generated wording (see SpeechBatchJob.dialogue_entries[].corrected_text).

WER is computed per-segment and aggregated (weighted by each segment's word
count) rather than over a whole transcript at once: the O(N*M) edit-distance
matrix is fine for a single segment's handful of words, but a multi-thousand-
word hearing transcript makes it computationally infeasible (a real 158-
segment/~14,600-word transcript took over 30 seconds against a whole-document
comparison in testing).
"""

from __future__ import annotations

import re

from rapidfuzz.distance import Levenshtein

_WORD_RE = re.compile(r"\S+")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _edit_distance(ref_words: list[str], hyp_words: list[str]) -> int:
    # Rolling 2-row DP rather than a full (n+1)x(m+1) matrix — only the
    # previous row is ever needed to compute the next one, so memory stays
    # O(min(n, m)) instead of O(n*m). Iterating over the shorter list keeps
    # each row as small as possible.
    if len(ref_words) < len(hyp_words):
        ref_words, hyp_words = hyp_words, ref_words
    n, m = len(ref_words), len(hyp_words)

    previous_row = list(range(m + 1))
    for i in range(1, n + 1):
        current_row = [i] + [0] * m
        for j in range(1, m + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                current_row[j] = previous_row[j - 1]
            else:
                current_row[j] = 1 + min(
                    previous_row[j],  # deletion
                    current_row[j - 1],  # insertion
                    previous_row[j - 1],  # substitution
                )
        previous_row = current_row

    return previous_row[m]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Return WER of hypothesis against reference, as a fraction (0.0-1.0+).

    Standard word-level Levenshtein distance: WER = (S + D + I) / N, where
    N is the reference word count. Can exceed 1.0 if the hypothesis has far
    more insertions than the reference has words. Only suitable for
    single-segment-scale text — see module docstring.
    """
    ref_words = _tokenize(reference)
    hyp_words = _tokenize(hypothesis)

    if not ref_words:
        return 0.0 if not hyp_words else 1.0

    return _edit_distance(ref_words, hyp_words) / len(ref_words)


def aggregate_word_error_rate(pairs: list[tuple[str, str]]) -> float:
    """WER across multiple (reference, hypothesis) segment pairs.

    Weighted by each segment's reference word count, so a handful of
    heavily-edited short segments don't skew the result the same as one
    long, mostly-correct one.
    """
    total_errors = 0
    total_ref_words = 0
    for reference, hypothesis in pairs:
        ref_words = _tokenize(reference)
        hyp_words = _tokenize(hypothesis)
        total_ref_words += len(ref_words)
        total_errors += _edit_distance(ref_words, hyp_words)

    if total_ref_words == 0:
        return 0.0
    return total_errors / total_ref_words


def baseline_word_error_rate(reference: str, hypothesis: str) -> float:
    """WER of hypothesis against a clerk-supplied baseline reference transcript.

    Unlike word_error_rate/aggregate_word_error_rate above, there's no
    smaller unit to compare here: a baseline transcript is uploaded as one
    whole document with no segment-level alignment to the auto-generated
    dialogue entries, so the comparison has to be whole-document. That's
    exactly the scale the module docstring says the naive O(N*M) DP in
    _edit_distance can't handle (30+ seconds on a real ~14,600-word
    transcript). rapidfuzz's Levenshtein.distance uses a bit-parallel
    (Myers') algorithm — O(N*M/64) — that runs the same comparison in
    well under a second, and it operates directly on sequences of tokens
    (not just characters), so words can be passed in as-is.
    """
    ref_words = _tokenize(reference)
    hyp_words = _tokenize(hypothesis)

    if not ref_words:
        return 0.0 if not hyp_words else 1.0

    return Levenshtein.distance(ref_words, hyp_words) / len(ref_words)

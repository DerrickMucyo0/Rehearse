"""Deterministic measurements of the original recognized transcript, never edits.

V1 words are maximal Unicode alphanumeric runs (underscore excluded), with
internal straight/curly apostrophes or ASCII hyphens joining adjacent runs.
Thus contractions and hyphenated words count once; punctuation alone does not.
The rule is identical for every language; these are recognized text tokens,
not a language-specific claim about linguistic word segmentation.

Timings arrive after the transcription adapter's word-type filtering. Require
one lexical token per eligible timing and an exact, case-sensitive token-sequence
match with the transcript. Ignore punctuation-only entries. Do not infer coverage
from counts alone, interpolate omitted words, or infer full recording duration.
Both timing metrics are unavailable unless coverage and intervals are usable.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

_WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")


class RecognizedWordTiming(Protocol):
    text: str
    start: float
    end: float


TimingUnavailableReason = Literal[
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
]


class SpeakingMetrics(BaseModel):
    """Ephemeral original-transcription measurements, not edited-answer metrics.

    Null is unavailable, never a substitute zero. Timing estimates exclude any
    leading/trailing recording silence. Filler eligibility means the existing
    normalized language code is exactly 'eng'; no confidence score is inferred.
    """

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    source: Literal["original_transcription"] = "original_transcription"
    recognized_word_count: Annotated[int, Field(ge=0)]
    um_count: Annotated[int, Field(ge=0)] | None
    uh_count: Annotated[int, Field(ge=0)] | None
    filler_unavailable_reason: Literal["unsupported_language"] | None
    timed_utterance_span_seconds: Annotated[float, Field(gt=0)] | None
    estimated_words_per_minute: Annotated[float, Field(gt=0)] | None
    timing_unavailable_reason: TimingUnavailableReason | None


def measure_transcription(
    text: str, language: str | None, words: Sequence[RecognizedWordTiming],
) -> SpeakingMetrics:
    """Pure calculation using only normalized original-provider data.

    English fillers are exact tokens 'um' and 'uh' after casefolding under the
    same word rule. Longer forms and context-dependent fillers are excluded.
    WPM = recognized_word_count * 60 / (last eligible end - first eligible start).
    No rounding, persistence, provider requests, logs, or edited-answer input.
    """
    tokens = _WORD.findall(text)
    english = language == "eng"
    folded = [token.casefold() for token in tokens]
    span, pace, unavailable = _timing_metrics(tokens, words)
    return SpeakingMetrics(
        recognized_word_count=len(tokens),
        um_count=folded.count("um") if english else None,
        uh_count=folded.count("uh") if english else None,
        filler_unavailable_reason=None if english else "unsupported_language",
        timed_utterance_span_seconds=span,
        estimated_words_per_minute=pace,
        timing_unavailable_reason=unavailable,
    )


def _timing_metrics(
    tokens: list[str], words: Sequence[RecognizedWordTiming],
) -> tuple[float | None, float | None, TimingUnavailableReason | None]:
    validated, unavailable = _validated_timings(tokens, words)
    if validated is None:
        return None, None, unavailable
    return validated.span, validated.pace, None


@dataclass(frozen=True)
class ValidatedTimings:
    """Call-local lexical intervals and the unchanged speaking-v1 timing values."""

    words: tuple[RecognizedWordTiming, ...]
    span: float
    pace: float


def validate_transcription_timings(
    text: str, words: Sequence[RecognizedWordTiming],
) -> tuple[ValidatedTimings | None, TimingUnavailableReason | None]:
    """Share speaking-v1 eligibility and failure precedence with derived metrics."""
    return _validated_timings(_WORD.findall(text), words)


def _validated_timings(
    tokens: list[str], words: Sequence[RecognizedWordTiming],
) -> tuple[ValidatedTimings | None, TimingUnavailableReason | None]:
    eligible = []
    timed_tokens = []
    for word in words:
        lexical = _WORD.findall(word.text)
        if not lexical:
            continue
        if len(lexical) != 1:
            return None, "timing_coverage_mismatch"
        # Normal WordTiming validation already guarantees these interval rules;
        # keep the pure function defensive against unvalidated internal inputs.
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (word.start, word.end)) or word.end < word.start:
            return None, "invalid_timing"
        if eligible and word.start < eligible[-1].end:
            return None, "invalid_timing_order"
        eligible.append(word)
        timed_tokens.append(lexical[0])
    if not eligible:
        return None, "missing_timings"
    if timed_tokens != tokens:
        return None, "timing_coverage_mismatch"
    span = float(eligible[-1].end - eligible[0].start)
    if span <= 0 or not math.isfinite(span):
        return None, "unusable_span"
    pace = len(tokens) * 60 / span
    if not math.isfinite(pace) or pace <= 0:
        return None, "unusable_span"
    return ValidatedTimings(tuple(eligible), span, pace), None

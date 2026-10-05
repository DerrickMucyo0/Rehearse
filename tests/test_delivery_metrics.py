"""Offline contracts for operational inter-word gaps, without delivery judgments."""

from collections.abc import Sequence
from decimal import Decimal, DefaultContext, Inexact, ROUND_DOWN, Rounded, localcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.delivery_metrics import (
    PAUSE_METRICS_VERSION,
    PAUSE_THRESHOLD_SECONDS,
    DeliveryMetrics,
    measure_delivery,
)
from app.speaking_metrics import measure_transcription
from app.transcription import TranscriptionResult, WordTiming


def timing(text, start=0.0, end=1.0):
    return WordTiming(text=text, start=start, end=end)


def raw_timing(text, start=0.0, end=1.0):
    return SimpleNamespace(text=text, start=start, end=end)


def assert_available(result, count, total, longest):
    assert isinstance(result, DeliveryMetrics)
    assert result.version == "pause-metrics-v1"
    assert result.pause_count == count
    assert result.total_pause_duration_seconds == total
    assert result.longest_pause_seconds == longest
    assert result.unavailable_reason is None


def assert_unavailable(result, reason):
    assert result.version == "pause-metrics-v1"
    assert result.pause_count is None
    assert result.total_pause_duration_seconds is None
    assert result.longest_pause_seconds is None
    assert result.unavailable_reason == reason


def test_version_and_threshold_lock_decimal_operational_definition():
    assert PAUSE_METRICS_VERSION == "pause-metrics-v1"
    assert type(PAUSE_THRESHOLD_SECONDS) is Decimal
    assert PAUSE_THRESHOLD_SECONDS == Decimal("0.50")


@pytest.mark.parametrize("text", ["", " \t\n", "... ! — _"])
def test_no_lexical_words_are_unavailable_not_measured_zero(text):
    assert_unavailable(measure_delivery(text, []), "missing_timings")


def test_punctuation_only_timing_entries_leave_analysis_unavailable():
    assert_unavailable(measure_delivery("!", [timing("!", 0.0, 1.0)]), "missing_timings")


def test_one_positive_usable_lexical_interval_has_measured_zero_pauses():
    assert_available(measure_delivery("word", [timing("word", 2.0, 2.5)]), 0, 0.0, 0.0)


def test_contiguous_lexical_intervals_have_measured_zero_pauses():
    result = measure_delivery("one two three", [
        timing("one", 0.0, 0.2), timing("two", 0.2, 0.4), timing("three", 0.4, 0.8),
    ])
    assert_available(result, 0, 0.0, 0.0)


@pytest.mark.parametrize(("gap", "count"), [
    (0.0, 0), (0.1, 0), (0.499999, 0), (0.500000, 1), (0.500001, 1), (2.75, 1),
])
def test_inclusive_boundary_and_whole_qualifying_gap(gap, count):
    words = [timing("one", 0.0, 0.0), timing("two", gap, gap + 1.0)]
    result = measure_delivery("one two", words)
    duration = gap if count else 0.0
    assert_available(result, count, duration, duration)


@pytest.mark.parametrize(("previous_end", "next_start", "count", "duration"), [
    (0.2, 0.7, 1, 0.5),
    (0.200001, 0.7, 0, 0.0),
    (0.199999, 0.7, 1, 0.500001),
    (1.1, 1.6, 1, 0.5),
])
def test_decimal_endpoint_subtraction_controls_boundary(previous_end, next_start, count, duration):
    result = measure_delivery("one two", [
        timing("one", 0.0, previous_end), timing("two", next_start, next_start + 0.2),
    ])
    assert_available(result, count, duration, duration)


def test_float_boundary_drift_fixture_is_actually_below_threshold_under_float_subtraction():
    assert 0.7 - 0.2 < 0.5
    result = measure_delivery("one two", [timing("one", 0.0, 0.2), timing("two", 0.7, 1.0)])
    assert_available(result, 1, 0.5, 0.5)


def test_multiple_qualifying_gaps_sum_full_duration_and_select_longest():
    result = measure_delivery("one two three four five", [
        timing("one", 0.0, 0.2),
        timing("two", 0.7, 0.9),
        timing("three", 1.0, 1.2),
        timing("four", 2.7, 3.0),
        timing("five", 5.75, 6.0),
    ])
    assert_available(result, 3, 4.75, 2.75)


def test_sum_uses_decimal_aggregate_before_final_float_conversion():
    result = measure_delivery("one two three", [
        timing("one", 0.0, 0.2), timing("two", 0.8, 0.9), timing("three", 1.6, 2.0),
    ])
    assert_available(result, 2, float(Decimal("0.6") + Decimal("0.7")), 0.7)
    assert result.total_pause_duration_seconds == 1.3
    assert result.total_pause_duration_seconds != 0.6 + 0.7


def test_very_high_precision_endpoint_values_are_not_rounded_before_arithmetic():
    previous_end = 1.2345678901234567
    next_start = 2.234567890123458
    expected = float(Decimal(str(next_start)) - Decimal(str(previous_end)))
    result = measure_delivery("one two", [
        timing("one", 0.0, previous_end), timing("two", next_start, 3.0),
    ])
    assert_available(result, 1, expected, expected)
    assert result.total_pause_duration_seconds != round(expected, 6)


def test_unrelated_decimal_context_cannot_change_the_versioned_boundary_or_values():
    words = [timing("one", 0.0, 0.200001), timing("two", 0.7, 1.0)]
    normal = measure_delivery("one two", words)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.Emin = -1
        context.Emax = 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.clear_flags()
        before = (context.prec, context.rounding, context.Emin, context.Emax,
                  dict(context.traps), dict(context.flags))
        constrained = measure_delivery("one two", words)
        after = (context.prec, context.rounding, context.Emin, context.Emax,
                 dict(context.traps), dict(context.flags))
        assert after == before
    assert constrained == normal
    assert_available(constrained, 0, 0.0, 0.0)


def test_mixed_exponents_do_not_round_a_genuinely_subthreshold_gap_up_to_boundary():
    # Default decimal precision would round 0.5 - 1E-100 back to 0.5.
    # The versioned arithmetic must preserve that it is strictly less than 0.5.
    words = [timing("one", 0.0, 1e-100), timing("two", 0.5, 1.0)]
    assert_available(measure_delivery("one two", words), 0, 0.0, 0.0)


def test_small_and_large_exponent_endpoints_keep_exact_boundary_classification():
    words = [timing("one", 0.0, 1e-100), timing("two", 0.5, 1.0), timing("three", 1e100, 1e100)]
    assert_available(measure_delivery("one two three", words), 1, 1e100, 1e100)


def test_mutated_default_decimal_context_cannot_change_positive_high_precision_results():
    words = [
        timing("one", 0.0, 1.2345678901234567),
        timing("two", 2.234567890123458, 2.5),
        timing("three", 3.1, 3.3),
    ]
    expected = measure_delivery("one two three", words)
    saved = DefaultContext.copy()
    try:
        DefaultContext.prec = 2
        DefaultContext.rounding = ROUND_DOWN
        DefaultContext.Emin = -1
        DefaultContext.Emax = 1
        DefaultContext.clamp = 1
        DefaultContext.traps[Inexact] = True
        DefaultContext.traps[Rounded] = True
        DefaultContext.flags[Inexact] = True
        DefaultContext.flags[Rounded] = True
        before = (DefaultContext.prec, DefaultContext.rounding, DefaultContext.Emin, DefaultContext.Emax,
                  DefaultContext.clamp, dict(DefaultContext.traps), dict(DefaultContext.flags))
        result = measure_delivery("one two three", words)
        after = (DefaultContext.prec, DefaultContext.rounding, DefaultContext.Emin, DefaultContext.Emax,
                 DefaultContext.clamp, dict(DefaultContext.traps), dict(DefaultContext.flags))
        assert after == before
        assert result == expected
        assert_available(result, 2, float(Decimal("1.6000000000000013")), float(Decimal("1.0000000000000013")))
    finally:
        DefaultContext.prec = saved.prec
        DefaultContext.rounding = saved.rounding
        DefaultContext.Emin = saved.Emin
        DefaultContext.Emax = saved.Emax
        DefaultContext.capitals = saved.capitals
        DefaultContext.clamp = saved.clamp
        DefaultContext.traps = saved.traps.copy()
        DefaultContext.flags = saved.flags.copy()


def test_measured_zero_and_unavailable_serialize_distinctly():
    available = measure_delivery("one", [timing("one", 0.0, 1.0)]).model_dump()
    unavailable = measure_delivery("one", []).model_dump()
    assert available["pause_count"] == 0
    assert available["total_pause_duration_seconds"] == 0.0
    assert available["longest_pause_seconds"] == 0.0
    assert unavailable["pause_count"] is None
    assert unavailable["total_pause_duration_seconds"] is None
    assert unavailable["longest_pause_seconds"] is None
    assert available["unavailable_reason"] is None
    assert unavailable["unavailable_reason"] == "missing_timings"


@pytest.mark.parametrize(("text", "words", "reason"), [
    ("one two", [], "missing_timings"),
    ("one two", [timing("one")], "timing_coverage_mismatch"),
    ("one two", [timing("two")], "timing_coverage_mismatch"),
    ("one two", [timing("one"), timing("other", 1.0, 2.0)], "timing_coverage_mismatch"),
    ("one two", [timing("one"), timing("two", 1.0, 2.0), timing("extra", 2.0, 3.0)], "timing_coverage_mismatch"),
    ("one two", [timing("one two")], "timing_coverage_mismatch"),
    ("one two", [timing("One"), timing("two", 1.0, 2.0)], "timing_coverage_mismatch"),
    ("", [timing("one")], "timing_coverage_mismatch"),
])
def test_missing_or_mismatched_lexical_coverage_does_not_invent_pause_values(text, words, reason):
    assert_unavailable(measure_delivery(text, words), reason)


@pytest.mark.parametrize(("start", "end"), [
    (-1.0, 1.0), (0.0, -1.0), (2.0, 1.0),
    (float("nan"), 1.0), (0.0, float("nan")),
    (float("inf"), 1.0), (0.0, float("inf")), (float("-inf"), 1.0),
    (True, 1.0), (0.0, False), ("0", 1.0), (0.0, "1"), (Decimal("0"), 1.0),
])
def test_invalid_unvalidated_lexical_interval_is_unavailable(start, end):
    assert_unavailable(measure_delivery("one", [raw_timing("one", start, end)]), "invalid_timing")


@pytest.mark.parametrize("words", [
    [timing("one", 2.0, 3.0), timing("two", 0.0, 1.0)],
    [timing("one", 0.0, 2.0), timing("two", 1.0, 3.0)],
    [timing("one", 0.0, 1.0), timing("two", 0.0, 1.0)],
])
def test_unordered_or_overlapping_intervals_are_not_sorted_or_clamped(words):
    before = [word.model_dump() for word in words]
    assert_unavailable(measure_delivery("one two", words), "invalid_timing_order")
    assert [word.model_dump() for word in words] == before


@pytest.mark.parametrize(("start", "end"), [(0.0, 0.0), (2.0, 2.0), (0.0, 5e-324)])
def test_unusable_span_or_overflowing_existing_pace_stays_unavailable(start, end):
    assert_unavailable(measure_delivery("one", [timing("one", start, end)]), "unusable_span")


@pytest.mark.parametrize(("text", "words", "reason"), [
    ("one two", [raw_timing("one two", float("nan"), 1.0)], "timing_coverage_mismatch"),
    ("one other", [timing("one", 2.0, 3.0), timing("two", 0.0, 1.0)], "invalid_timing_order"),
    ("one other", [timing("one"), raw_timing("two", -1.0, 2.0)], "invalid_timing"),
    ("one two", [raw_timing("one", -1.0, 1.0), timing("two three", 2.0, 3.0)], "invalid_timing"),
])
def test_first_existing_validation_failure_retains_reason_precedence(text, words, reason):
    assert_unavailable(measure_delivery(text, words), reason)
    assert measure_transcription(text, "eng", words).timing_unavailable_reason == reason


def test_punctuation_entries_do_not_change_lexical_gaps_or_require_their_own_interval_validation():
    # This locks the existing pure validator behavior, after adapter normalization.
    words = [
        raw_timing("!", -1.0, float("nan")), timing("one", 5.0, 5.2),
        raw_timing("...", 0.0, 99.0), timing("two.", 5.7, 6.0), raw_timing("_", 99.0, 98.0),
    ]
    assert_available(measure_delivery("one two.", words), 1, 0.5, 0.5)


@pytest.mark.parametrize(("text", "timed_texts"), [
    ("café 中文 123", ["café", "中文", "123"]),
    ("I'm don't", ["I'm", "don't"]),
    ("we’re here", ["we’re", "here"]),
    ("mother-in-law well-known", ["mother-in-law", "well-known"]),
    ("one_two", ["one", "two"]),
    ("one—two three/four", ["one", "two", "three", "four"]),
])
def test_pause_eligibility_uses_existing_unicode_apostrophe_hyphen_and_separator_rules(text, timed_texts):
    words = [timing(word, index * 0.75, index * 0.75 + 0.25) for index, word in enumerate(timed_texts)]
    count = len(timed_texts) - 1
    assert_available(measure_delivery(text, words), count, count * 0.5, 0.5)
    assert measure_transcription(text, "eng", words).recognized_word_count == len(timed_texts)


@pytest.mark.parametrize("language", ["eng", "fra", None])
def test_um_and_uh_remain_lexical_regardless_of_filler_availability(language):
    words = [timing("Um", 0.0, 0.25), timing("uh", 0.75, 1.0), timing("umbrella", 1.5, 1.75)]
    assert_available(measure_delivery("Um uh umbrella", words), 2, 1.0, 0.5)
    speaking = measure_transcription("Um uh umbrella", language, words)
    assert speaking.recognized_word_count == 3
    assert (speaking.um_count, speaking.uh_count) == ((1, 1) if language == "eng" else (None, None))


def test_input_transcript_and_timing_models_are_not_mutated_or_retained_in_result():
    transcription = TranscriptionResult(text="PRIVATE-CONTENT one", language="eng", words=[
        timing("PRIVATE-CONTENT", 0.0, 0.2), timing("one", 0.7, 1.0),
    ])
    before = transcription.model_dump()
    result = measure_delivery(transcription.text, transcription.words)
    assert transcription.model_dump() == before
    assert set(result.model_dump()) == {
        "version", "pause_count", "total_pause_duration_seconds", "longest_pause_seconds", "unavailable_reason",
    }
    assert all(value is None or type(value) in (str, int, float) for value in result.model_dump().values())
    serialized = result.model_dump_json()
    assert "PRIVATE-CONTENT" not in serialized
    assert "word" not in serialized
    assert "events" not in serialized
    assert "measurement_id" not in serialized
    assert "session_id" not in serialized


def test_measurement_is_immutable_and_forbids_arbitrary_payload_fields():
    result = measure_delivery("one", [timing("one")])
    with pytest.raises(ValidationError):
        result.pause_count = 3
    with pytest.raises(ValidationError):
        DeliveryMetrics.model_validate({**result.model_dump(), "provider_payload": {"private": "content"}})


@pytest.mark.parametrize(("field", "value"), [
    ("version", "pause-metrics-v2"), ("pause_count", -1), ("pause_count", True), ("pause_count", "0"),
    ("total_pause_duration_seconds", -1.0), ("longest_pause_seconds", float("inf")),
    ("total_pause_duration_seconds", float("nan")), ("unavailable_reason", "new_reason"),
])
def test_result_schema_rejects_wrong_version_types_bounds_or_reason(field, value):
    data = measure_delivery("one", [timing("one")]).model_dump()
    with pytest.raises(ValidationError):
        DeliveryMetrics.model_validate({**data, field: value})


@pytest.mark.parametrize("changes", [
    {"pause_count": None},
    {"total_pause_duration_seconds": None},
    {"longest_pause_seconds": None},
    {"pause_count": None, "total_pause_duration_seconds": None, "longest_pause_seconds": None},
    {"unavailable_reason": "missing_timings"},
    {"total_pause_duration_seconds": 1.0},
    {"longest_pause_seconds": 1.0},
    {"pause_count": 1},
    {"pause_count": 1, "total_pause_duration_seconds": 0.5, "longest_pause_seconds": 0.75},
])
def test_result_schema_enforces_complete_availability_and_zero_positive_consistency(changes):
    data = measure_delivery("one", [timing("one")]).model_dump()
    with pytest.raises(ValidationError):
        DeliveryMetrics.model_validate({**data, **changes})


def test_engine_has_no_network_database_file_or_content_logging_effects(monkeypatch, capsys, caplog):
    import builtins
    import socket

    import httpx
    import sqlalchemy

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure measurement must not perform external or storage operations")

    words = [timing("PRIVATE-CONTENT", 0.0, 0.2), timing("two", 0.7, 1.0)]
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert_available(measure_delivery("PRIVATE-CONTENT two", words), 1, 0.5, 0.5)
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert caplog.records == []


@pytest.mark.parametrize(("text", "words", "span", "pace"), [
    ("One, two three!", [timing("One,", 5.0, 5.5), timing("two", 5.5, 6.0), timing("three!", 7.0, 8.0)], 3.0, 60.0),
    ("one", [timing("one", 0.0, 120.0)], 120.0, 0.5),
    ("one two", [timing("one", 0.0, 0.2), timing("two", 0.7, 1.0)], 1.0, 120.0),
    ("one two", [timing("one", 0.1, 0.2), timing("two", 0.7, 1.3)], 1.3 - 0.1, 120 / (1.3 - 0.1)),
])
def test_existing_speaking_span_and_wpm_are_float_for_float_unchanged(text, words, span, pace):
    before = measure_transcription(text, "eng", words)
    measure_delivery(text, words)
    after = measure_transcription(text, "eng", words)
    assert after.model_dump() == before.model_dump()
    assert after.timed_utterance_span_seconds == span
    assert after.estimated_words_per_minute == pace
    assert after.timing_unavailable_reason is None


@pytest.mark.parametrize(("text", "words", "reason"), [
    ("one", [], "missing_timings"),
    ("one two", [timing("one")], "timing_coverage_mismatch"),
    ("one", [raw_timing("one", -1.0, 1.0)], "invalid_timing"),
    ("one two", [timing("one", 0.0, 2.0), timing("two", 1.0, 3.0)], "invalid_timing_order"),
    ("one", [timing("one", 0.0, 5e-324)], "unusable_span"),
])
def test_all_existing_speaking_unavailable_reasons_and_public_fields_remain_unchanged(text, words, reason):
    before = measure_transcription(text, "eng", words)
    assert_unavailable(measure_delivery(text, words), reason)
    after = measure_transcription(text, "eng", words)
    assert after.model_dump() == before.model_dump()
    assert after.timing_unavailable_reason == reason
    assert after.timed_utterance_span_seconds is after.estimated_words_per_minute is None
    assert set(after.model_dump()) == {
        "source", "recognized_word_count", "um_count", "uh_count", "filler_unavailable_reason",
        "timed_utterance_span_seconds", "estimated_words_per_minute", "timing_unavailable_reason",
    }


def test_two_thousand_words_use_linear_input_access_without_pairwise_processing():
    accesses = {"text": 0, "start": 0, "end": 0, "items": 0}

    class CountedTiming:
        def __init__(self, index):
            self.index = index

        @property
        def text(self):
            accesses["text"] += 1
            return f"word{self.index}"

        @property
        def start(self):
            accesses["start"] += 1
            return self.index * 0.75

        @property
        def end(self):
            accesses["end"] += 1
            return self.index * 0.75 + 0.25

    class CountedSequence(Sequence):
        def __init__(self, size):
            self.values = [CountedTiming(index) for index in range(size)]

        def __len__(self):
            return len(self.values)

        def __getitem__(self, index):
            accesses["items"] += 1
            return self.values[index]

    count = 2000
    words = CountedSequence(count)
    result = measure_delivery(" ".join(f"word{index}" for index in range(count)), words)
    assert_available(result, count - 1, (count - 1) * 0.5, 0.5)
    # A generous constant permits validation and adjacent scans; a pairwise scan
    # would make millions of accesses instead of a constant multiple of n.
    assert accesses["items"] <= count * 4 + 4
    assert accesses["text"] <= count * 4
    assert accesses["start"] + accesses["end"] <= count * 30

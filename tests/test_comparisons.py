from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.comparisons import (
    AttemptComparison, ComparedAttempt, ComparisonMetrics, MeasurementSnapshot,
    compare_measurements,
)


METRIC_NAMES = (
    "recognized_word_count", "um_count", "uh_count",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
)
CHANGE_FIELDS = {
    "before", "after", "delta", "before_unavailable_reason",
    "after_unavailable_reason", "comparable", "comparison_unavailable_reason",
}
TIMING_REASONS = (
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
)


def measurement(**changes):
    values = {
        "measurement_version": "speaking-metrics-v1",
        "measurement_source": "original_transcription",
        "recognized_word_count": 10,
        "um_count": 2,
        "uh_count": 1,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 12.123456789123,
        "estimated_words_per_minute": 49.491231198,
        "timing_unavailable_reason": None,
    }
    values.update(changes)
    return MeasurementSnapshot(**values)


def changes(comparison):
    return [getattr(comparison, name) for name in METRIC_NAMES]


def test_exact_persisted_values_and_signed_deltas_for_all_five_metrics():
    before = measurement()
    after = measurement(
        recognized_word_count=13, um_count=0, uh_count=4,
        timed_utterance_span_seconds=12.123456789789,
        estimated_words_per_minute=48.891231199,
    )
    comparison = compare_measurements(before, after)
    for name in METRIC_NAMES:
        change = getattr(comparison, name)
        assert change.before == getattr(before, name)
        assert change.after == getattr(after, name)
        assert change.delta == getattr(after, name) - getattr(before, name)
        assert change.before_unavailable_reason is None
        assert change.after_unavailable_reason is None
        assert change.comparable is True
        assert change.comparison_unavailable_reason is None
    assert comparison.um_count.delta == -2
    assert comparison.estimated_words_per_minute.delta < 0
    # A display rounding step would erase this real difference.
    assert comparison.timed_utterance_span_seconds.delta != 0
    assert round(after.timed_utterance_span_seconds, 3) == round(
        before.timed_utterance_span_seconds, 3,
    )


@pytest.mark.parametrize("name", ["recognized_word_count", "um_count", "uh_count"])
def test_measured_zero_remains_available_and_is_not_no_measurement(name):
    before = measurement(**{name: 0})
    after = measurement(**{name: 0})
    change = getattr(compare_measurements(before, after), name)
    assert change.before == 0
    assert change.after == 0
    assert change.delta == 0
    assert type(change.before) is int
    assert change.comparable is True
    assert change.before_unavailable_reason is None
    assert change.after_unavailable_reason is None


@pytest.mark.parametrize(("before_present", "after_present", "reason"), [
    (False, True, "before_unavailable"),
    (True, False, "after_unavailable"),
    (False, False, "both_unavailable"),
])
def test_typed_attempts_never_invent_measurements_or_zero(
    before_present, after_present, reason,
):
    before = measurement() if before_present else None
    after = measurement() if after_present else None
    comparison = compare_measurements(before, after)
    for name, change in zip(METRIC_NAMES, changes(comparison), strict=True):
        assert change.before == (getattr(before, name) if before_present else None)
        assert change.after == (getattr(after, name) if after_present else None)
        assert change.before_unavailable_reason == (None if before_present else "no_measurement")
        assert change.after_unavailable_reason == (None if after_present else "no_measurement")
        assert change.delta is None
        assert change.comparable is False
        assert change.comparison_unavailable_reason == reason


@pytest.mark.parametrize(("before_unavailable", "after_unavailable", "reason"), [
    (True, False, "before_unavailable"),
    (False, True, "after_unavailable"),
    (True, True, "both_unavailable"),
])
def test_unsupported_filler_metrics_preserve_each_side_reason(
    before_unavailable, after_unavailable, reason,
):
    unsupported = {"um_count": None, "uh_count": None,
                   "filler_unavailable_reason": "unsupported_language"}
    before = measurement(**unsupported) if before_unavailable else measurement()
    after = measurement(**unsupported) if after_unavailable else measurement()
    comparison = compare_measurements(before, after)
    for name in ("um_count", "uh_count"):
        change = getattr(comparison, name)
        assert change.before == (None if before_unavailable else getattr(before, name))
        assert change.after == (None if after_unavailable else getattr(after, name))
        assert change.before_unavailable_reason == (
            "unsupported_language" if before_unavailable else None
        )
        assert change.after_unavailable_reason == (
            "unsupported_language" if after_unavailable else None
        )
        assert change.comparable is False
        assert change.delta is None
        assert change.comparison_unavailable_reason == reason
    assert comparison.recognized_word_count.comparable is True
    assert comparison.timed_utterance_span_seconds.comparable is True


@pytest.mark.parametrize("timing_reason", TIMING_REASONS)
@pytest.mark.parametrize("unavailable_side", ["before", "after"])
def test_all_existing_timing_reasons_are_preserved_without_recalculation(
    timing_reason, unavailable_side,
):
    unavailable = measurement(
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason=timing_reason,
    )
    before, after = (
        (unavailable, measurement()) if unavailable_side == "before" else
        (measurement(), unavailable)
    )
    comparison = compare_measurements(before, after)
    for name in ("timed_utterance_span_seconds", "estimated_words_per_minute"):
        change = getattr(comparison, name)
        assert getattr(change, unavailable_side) is None
        assert getattr(change, f"{unavailable_side}_unavailable_reason") == timing_reason
        assert change.delta is None
        assert change.comparable is False
        assert change.comparison_unavailable_reason == f"{unavailable_side}_unavailable"
    assert comparison.recognized_word_count.comparable is True
    assert comparison.um_count.comparable is True


def test_different_unavailable_reasons_remain_distinguishable_on_each_side():
    before = measurement(
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason="invalid_timing",
    )
    after = measurement(
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason="missing_timings",
    )
    for change in (
        compare_measurements(before, after).timed_utterance_span_seconds,
        compare_measurements(before, after).estimated_words_per_minute,
    ):
        assert change.before is None
        assert change.after is None
        assert change.before_unavailable_reason == "invalid_timing"
        assert change.after_unavailable_reason == "missing_timings"
        assert change.delta is None
        assert change.comparison_unavailable_reason == "both_unavailable"


def test_measurement_version_mismatch_prevents_every_delta():
    before = measurement()
    after = measurement(measurement_version="speaking-metrics-v2")
    for change in changes(compare_measurements(before, after)):
        assert change.before is not None
        assert change.after is not None
        assert change.delta is None
        assert change.comparable is False
        assert change.comparison_unavailable_reason == "measurement_version_mismatch"


@pytest.mark.parametrize(("before_source", "after_source"), [
    ("original_transcription", "edited_answer"),
    ("edited_answer", "original_transcription"),
    ("edited_answer", "edited_answer"),
])
def test_only_original_transcription_sources_are_compatible(before_source, after_source):
    comparison = compare_measurements(
        measurement(measurement_source=before_source),
        measurement(measurement_source=after_source),
    )
    for change in changes(comparison):
        assert change.delta is None
        assert change.comparable is False
        assert change.comparison_unavailable_reason == "measurement_source_incompatible"


def test_compatibility_precedence_preserves_unavailability_without_inventing_values():
    before = measurement(
        measurement_version="speaking-metrics-v2", measurement_source="edited_answer",
        um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language",
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason="unusable_span",
    )
    comparison = compare_measurements(before, measurement())
    for change in changes(comparison):
        assert change.comparison_unavailable_reason == "measurement_version_mismatch"
        assert change.delta is None
    assert comparison.um_count.before is None
    assert comparison.um_count.before_unavailable_reason == "unsupported_language"
    assert comparison.estimated_words_per_minute.before_unavailable_reason == "unusable_span"


def test_source_incompatibility_precedes_unavailable_metric_sides():
    before = measurement(
        measurement_source="edited_answer", um_count=None, uh_count=None,
        filler_unavailable_reason="unsupported_language",
    )
    change = compare_measurements(before, measurement()).um_count
    assert change.comparison_unavailable_reason == "measurement_source_incompatible"
    assert change.before_unavailable_reason == "unsupported_language"
    assert change.delta is None


def test_missing_measurement_has_side_reason_without_claiming_definition_mismatch():
    change = compare_measurements(
        None, measurement(measurement_version="another-version", measurement_source="other-source"),
    ).recognized_word_count
    assert change.before_unavailable_reason == "no_measurement"
    assert change.comparison_unavailable_reason == "before_unavailable"


def test_response_contains_only_neutral_explicit_identity_and_metric_fields():
    before = ComparedAttempt(
        id=uuid4(), attempt_number=1, measurement_id=uuid4(),
        measurement_version="speaking-metrics-v1", measurement_source="original_transcription",
    )
    after = ComparedAttempt(
        id=uuid4(), attempt_number=3, measurement_id=None,
        measurement_version=None, measurement_source=None,
    )
    response = AttemptComparison(
        session_id=uuid4(), question_index=0, before_attempt=before,
        after_attempt=after, comparison=compare_measurements(measurement(), None),
    ).model_dump(mode="json")
    assert set(response) == {
        "session_id", "question_index", "before_attempt", "after_attempt", "comparison",
    }
    assert set(response["before_attempt"]) == {
        "id", "attempt_number", "measurement_id", "measurement_version", "measurement_source",
    }
    assert set(response["comparison"]) == set(METRIC_NAMES)
    for change in response["comparison"].values():
        assert set(change) == CHANGE_FIELDS
    assert "answer" not in response["before_attempt"]
    assert response["after_attempt"]["measurement_id"] is None
    assert response["after_attempt"]["measurement_version"] is None
    assert response["after_attempt"]["measurement_source"] is None


@pytest.mark.parametrize(("field", "value"), [
    ("recognized_word_count", True), ("recognized_word_count", -1),
    ("um_count", "0"), ("uh_count", -1),
    ("timed_utterance_span_seconds", float("nan")),
    ("estimated_words_per_minute", float("inf")),
    ("measurement_version", " \t"),
])
def test_snapshot_rejects_invalid_scalar_measurements(field, value):
    with pytest.raises(ValidationError):
        measurement(**{field: value})


def test_snapshots_and_output_reject_mutation_or_semantic_enrichment():
    snapshot = measurement()
    with pytest.raises(ValidationError):
        snapshot.recognized_word_count = 99
    with pytest.raises(ValidationError):
        ComparisonMetrics(**compare_measurements(snapshot, snapshot).model_dump(), score=100)

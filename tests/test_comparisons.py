from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.comparisons import (
    AttemptComparison, ComparedAttempt, ComparisonMetrics, DeliveryMetricChange, DeliverySnapshot,
    MeasurementSnapshot, compare_delivery_measurements, compare_measurements, delivery_snapshot,
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
        "delivery_comparison",
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
    assert response["delivery_comparison"] is None


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


DELIVERY_METRICS = ("pause_count", "total_pause_duration_seconds", "longest_pause_seconds")
DELIVERY_FIELDS = {*DELIVERY_METRICS, "version", "source", "unavailable_reason"}


def delivery(**changes):
    values = {
        "version": "pause-metrics-v1", "source": "original_transcription", "pause_count": 2,
        "total_pause_duration_seconds": 1.234567890123, "longest_pause_seconds": 0.734567890123,
        "unavailable_reason": None,
    }
    values.update(changes)
    return DeliverySnapshot(**values)


def delivery_side(state):
    if state == "typed":
        return None
    if state == "legacy":
        return measurement()
    if state == "unavailable":
        return measurement(delivery_metrics=delivery(
            pause_count=None, total_pause_duration_seconds=None, longest_pause_seconds=None,
            unavailable_reason="invalid_timing",
        ))
    return measurement(delivery_metrics=delivery())


@pytest.mark.parametrize(("before_zero", "after_zero"), [(True, True), (True, False), (False, True), (False, False)])
def test_delivery_exact_available_values_and_signed_unrounded_deltas(before_zero, after_zero):
    zero = delivery(pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0)
    before = zero if before_zero else delivery()
    after = zero if after_zero else delivery(
        pause_count=3, total_pause_duration_seconds=1.234567890789, longest_pause_seconds=0.734567890789,
    )
    result = compare_delivery_measurements(measurement(delivery_metrics=before), measurement(delivery_metrics=after))
    for name in DELIVERY_METRICS:
        change = getattr(result, name)
        assert change.model_dump() == {
            "before": getattr(before, name), "after": getattr(after, name),
            "delta": getattr(after, name) - getattr(before, name), "before_unavailable_reason": None,
            "after_unavailable_reason": None, "comparable": True, "comparison_unavailable_reason": None,
        }
    assert type(result.pause_count.before) is int
    if not before_zero and not after_zero:
        assert result.total_pause_duration_seconds.delta != 0
        assert round(before.total_pause_duration_seconds, 3) == round(after.total_pause_duration_seconds, 3)


@pytest.mark.parametrize("before_state", ["typed", "legacy", "available", "unavailable"])
@pytest.mark.parametrize("after_state", ["typed", "legacy", "available", "unavailable"])
def test_delivery_distinguishes_typed_legacy_recorded_unavailable_and_available(before_state, after_state):
    before, after = delivery_side(before_state), delivery_side(after_state)
    result = compare_delivery_measurements(before, after)
    reasons = {"typed": "no_measurement", "legacy": "not_recorded", "available": None,
               "unavailable": "invalid_timing"}
    before_available, after_available = before_state == "available", after_state == "available"
    unavailable = (
        None if before_available and after_available else "after_unavailable" if before_available else
        "before_unavailable" if after_available else "both_unavailable"
    )
    for name in DELIVERY_METRICS:
        change = getattr(result, name)
        assert change.before == (getattr(delivery(), name) if before_available else None)
        assert change.after == (getattr(delivery(), name) if after_available else None)
        assert change.before_unavailable_reason == reasons[before_state]
        assert change.after_unavailable_reason == reasons[after_state]
        assert change.delta == (0 if unavailable is None else None)
        assert change.comparable == (unavailable is None)
        assert change.comparison_unavailable_reason == unavailable
    for side, state in (("before", before_state), ("after", after_state)):
        recorded = state in {"available", "unavailable"}
        assert getattr(result, f"{side}_version") == ("pause-metrics-v1" if recorded else None)
        assert getattr(result, f"{side}_source") == ("original_transcription" if recorded else None)


@pytest.mark.parametrize("reason", TIMING_REASONS)
def test_delivery_preserves_each_approved_unavailable_reason(reason):
    unavailable = delivery(pause_count=None, total_pause_duration_seconds=None, longest_pause_seconds=None,
                           unavailable_reason=reason)
    result = compare_delivery_measurements(measurement(delivery_metrics=unavailable),
                                          measurement(delivery_metrics=delivery()))
    for name in DELIVERY_METRICS:
        assert getattr(result, name).before_unavailable_reason == reason
        assert getattr(result, name).delta is None


@pytest.mark.parametrize(("changes", "reason"), [
    ({"version": "pause-metrics-v2"}, "measurement_version_mismatch"),
    ({"source": "another_source"}, "measurement_source_incompatible"),
    ({"version": "pause-metrics-v2", "source": "another_source"}, "measurement_version_mismatch"),
])
def test_delivery_incompatibility_preserves_values_and_does_not_disable_speaking(changes, reason):
    before = measurement(delivery_metrics=delivery())
    after = measurement(delivery_metrics=delivery(**changes))
    assert compare_measurements(before, after) == compare_measurements(measurement(), measurement())
    result = compare_delivery_measurements(before, after)
    for name in DELIVERY_METRICS:
        change = getattr(result, name)
        assert change.before == change.after == getattr(delivery(), name)
        assert change.delta is None
        assert change.comparison_unavailable_reason == reason
        assert change.before_unavailable_reason is change.after_unavailable_reason is None


def test_speaking_version_mismatch_does_not_disable_compatible_delivery():
    before = measurement(delivery_metrics=delivery())
    after = measurement(measurement_version="speaking-metrics-historical", delivery_metrics=delivery())
    assert all(change.comparison_unavailable_reason == "measurement_version_mismatch"
               for change in changes(compare_measurements(before, after)))
    assert all(getattr(compare_delivery_measurements(before, after), name).comparable for name in DELIVERY_METRICS)


def test_equal_nonblank_delivery_sources_are_compatible_independently_of_speaking():
    before = measurement(measurement_source="other_source", delivery_metrics=delivery(source="other_source"))
    after = measurement(measurement_source="other_source", delivery_metrics=delivery(source="other_source"))
    assert all(change.comparison_unavailable_reason == "measurement_source_incompatible"
               for change in changes(compare_measurements(before, after)))
    assert all(getattr(compare_delivery_measurements(before, after), name).delta == 0 for name in DELIVERY_METRICS)


def test_delivery_compatibility_precedes_unavailable_sides_without_erasing_reason():
    before = measurement(delivery_metrics=delivery(
        version="historical", source="other_source", pause_count=None,
        total_pause_duration_seconds=None, longest_pause_seconds=None, unavailable_reason="missing_timings",
    ))
    result = compare_delivery_measurements(before, measurement(delivery_metrics=delivery()))
    for name in DELIVERY_METRICS:
        change = getattr(result, name)
        assert change.comparison_unavailable_reason == "measurement_version_mismatch"
        assert change.before_unavailable_reason == "missing_timings"
        assert change.before is change.delta is None


@pytest.mark.parametrize("values", [
    {"version": " \t"}, {"source": "\n"}, {"pause_count": True}, {"pause_count": -1},
    {"pause_count": None}, {"total_pause_duration_seconds": None}, {"longest_pause_seconds": None},
    {"unavailable_reason": "invalid_timing"}, {"unavailable_reason": "unsupported_language"},
    {"total_pause_duration_seconds": float("nan")}, {"longest_pause_seconds": float("inf")},
    {"total_pause_duration_seconds": -1.0}, {"longest_pause_seconds": 9.0},
    {"pause_count": 0}, {"total_pause_duration_seconds": 0.0}, {"longest_pause_seconds": 0.0},
    {"words": []}, {"pause_events": []}, {"measurement_id": str(uuid4())}, {"score": 100},
])
def test_delivery_snapshot_rejects_mixed_states_nonfinite_values_and_sensitive_enrichment(values):
    with pytest.raises(ValidationError):
        delivery(**values)


def test_delivery_projection_is_frozen_explicit_and_preserves_exact_provenance():
    snapshot = delivery(version=" historical ", source=" original_transcription ")
    assert set(snapshot.model_dump()) == DELIVERY_FIELDS
    assert snapshot.version == " historical "
    assert snapshot.source == " original_transcription "
    with pytest.raises(ValidationError):
        snapshot.pause_count = 0
    result = compare_delivery_measurements(measurement(delivery_metrics=snapshot), measurement(delivery_metrics=snapshot))
    assert set(result.model_dump()) == {*DELIVERY_METRICS, "before_version", "after_version", "before_source", "after_source"}
    for name in DELIVERY_METRICS:
        assert set(getattr(result, name).model_dump()) == CHANGE_FIELDS
    with pytest.raises(ValidationError):
        DeliveryMetricChange(**result.pause_count.model_dump(), better=True)


@pytest.mark.parametrize("index", range(4))
def test_scalar_projector_rejects_partial_legacy_rows(index):
    values = [None, None, None, None]
    values[index] = [0, 0.0, 0.0, "missing_timings"][index]
    with pytest.raises(ValueError):
        delivery_snapshot(None, "original_transcription", *values)
    assert delivery_snapshot(None, "original_transcription", None, None, None, None) is None

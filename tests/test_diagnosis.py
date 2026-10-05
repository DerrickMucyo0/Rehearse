"""Offline contract tests for projecting supplied facts into diagnosis context."""

import json
from typing import get_args
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.comparisons import (
    AttemptComparison,
    ComparedAttempt,
    ComparisonMetrics,
    DeliveryComparison,
    DeliveryMetricChange,
    DeliverySnapshot,
    MeasurementSnapshot,
    MetricChange,
    compare_delivery_measurements,
    compare_measurements,
)
from app.diagnosis import (
    DIAGNOSIS_CONTEXT_VERSION,
    DiagnosisContext,
    PreviousAttemptFacts,
    build_diagnosis_context,
)
from app.speaking_metrics import TimingUnavailableReason


QUESTION_INDEX = 2
ATTEMPT_NUMBER = 3
INVALID_COMPARISON_MESSAGE = (
    "Comparison must describe the immediately previous attempt for this question."
)
IDENTITY_SENTINELS = tuple(UUID(f"00000000-0000-4000-8000-{value:012d}") for value in range(1, 6))
SPEAKING_METRICS = (
    "recognized_word_count", "um_count", "uh_count",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
)
DELIVERY_METRICS = (
    "pause_count", "total_pause_duration_seconds", "longest_pause_seconds",
)
CHANGE_FIELDS = {
    "before", "after", "delta", "before_unavailable_reason",
    "after_unavailable_reason", "comparable", "comparison_unavailable_reason",
}


def delivery(**updates):
    values = {
        "version": "pause-metrics-v1", "source": "original_transcription",
        "pause_count": 2, "total_pause_duration_seconds": 1.234567890123,
        "longest_pause_seconds": 0.734567890123, "unavailable_reason": None,
    }
    values.update(updates)
    return DeliverySnapshot(**values)


def measurement(**updates):
    values = {
        "measurement_version": "speaking-metrics-v1",
        "measurement_source": "original_transcription",
        "recognized_word_count": 10, "um_count": 2, "uh_count": 1,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 12.123456789123,
        "estimated_words_per_minute": 49.491231198,
        "timing_unavailable_reason": None, "delivery_metrics": delivery(),
    }
    values.update(updates)
    return MeasurementSnapshot(**values)


def compared_attempt(snapshot, number, attempt_id, measurement_id):
    return ComparedAttempt(
        id=attempt_id, attempt_number=number,
        measurement_id=measurement_id if snapshot is not None else None,
        measurement_version=snapshot.measurement_version if snapshot is not None else None,
        measurement_source=snapshot.measurement_source if snapshot is not None else None,
    )


def comparison(before, after, **updates):
    session_id, before_id, after_id, before_measurement_id, after_measurement_id = IDENTITY_SENTINELS
    values = {
        "session_id": session_id, "question_index": QUESTION_INDEX,
        "before_attempt": compared_attempt(before, 2, before_id, before_measurement_id),
        "after_attempt": compared_attempt(after, 3, after_id, after_measurement_id),
        "comparison": compare_measurements(before, after),
        "delivery_comparison": compare_delivery_measurements(before, after),
    }
    values.update(updates)
    return AttemptComparison(**values)


def context(snapshot=None, supplied_comparison=None, **updates):
    values = {
        "question": "Describe a decision.", "answer": "I compared the options.",
        "question_index": QUESTION_INDEX, "attempt_number": ATTEMPT_NUMBER,
        "measurement": snapshot, "comparison": supplied_comparison,
    }
    values.update(updates)
    return build_diagnosis_context(**values)


def previous_facts():
    supplied = comparison(measurement(), measurement())
    return PreviousAttemptFacts(
        before_attempt_number=2, after_attempt_number=3,
        speaking=supplied.comparison, delivery=supplied.delivery_comparison,
    )


def test_first_attempt_without_measurement_or_comparison_has_explicit_null_facts():
    result = build_diagnosis_context(
        question="", answer="", question_index=0, attempt_number=1, measurement=None,
    )
    assert DIAGNOSIS_CONTEXT_VERSION == "diagnosis-context-v1"
    assert result.model_dump() == {
        "context_version": "diagnosis-context-v1", "question": "", "answer": "",
        "question_index": 0, "attempt_number": 1,
        "measurement": None, "previous_attempt": None,
    }
    assert result.previous_attempt is None


def test_measurement_without_comparison_is_preserved_by_identity():
    snapshot = measurement()
    original = snapshot.model_dump(mode="json")
    result = context(snapshot, None)
    assert result.measurement is snapshot
    assert result.previous_attempt is None
    assert result.model_dump(mode="json")["measurement"] == original
    assert snapshot.model_dump(mode="json") == original


def test_measured_speaking_zeros_remain_available_with_legacy_delivery_absent():
    snapshot = measurement(
        recognized_word_count=0, um_count=0, uh_count=0,
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason="missing_timings",
        delivery_metrics=None,
    )
    supplied = comparison(snapshot, snapshot)
    result = context(snapshot, supplied)
    assert result.measurement is snapshot
    for name in ("recognized_word_count", "um_count", "uh_count"):
        change = getattr(result.previous_attempt.speaking, name)
        assert change.before == change.after == change.delta == 0
        assert type(change.after) is int
        assert change.comparable is True
        assert change.after_unavailable_reason is None
    assert result.measurement.delivery_metrics is None
    for name in DELIVERY_METRICS:
        change = getattr(result.previous_attempt.delivery, name)
        assert change.before is change.after is change.delta is None
        assert change.before_unavailable_reason == "not_recorded"
        assert change.after_unavailable_reason == "not_recorded"


def test_typed_current_attempt_preserves_missing_measurement_reasons():
    supplied = comparison(measurement(), None)
    result = context(None, supplied)
    assert result.measurement is None
    for family, names in ((result.previous_attempt.speaking, SPEAKING_METRICS),
                          (result.previous_attempt.delivery, DELIVERY_METRICS)):
        for name in names:
            change = getattr(family, name)
            assert change.after is None
            assert change.after_unavailable_reason == "no_measurement"
            assert change.delta is None
            assert change.comparable is False
            assert change.comparison_unavailable_reason == "after_unavailable"
    assert result.previous_attempt.delivery.after_version is None
    assert result.previous_attempt.delivery.after_source is None


@pytest.mark.parametrize("unavailable_side", ["before", "after", "both"])
def test_unsupported_language_filler_reason_is_preserved_on_each_side(unavailable_side):
    unavailable = measurement(
        um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language",
    )
    before = unavailable if unavailable_side in {"before", "both"} else measurement()
    after = unavailable if unavailable_side in {"after", "both"} else measurement()
    supplied = comparison(before, after)
    result = context(after, supplied)
    assert result.measurement is after
    assert result.measurement.delivery_metrics is after.delivery_metrics
    assert result.previous_attempt.speaking is supplied.comparison
    for name in ("um_count", "uh_count"):
        change = getattr(result.previous_attempt.speaking, name)
        for side in ("before", "after"):
            expected = "unsupported_language" if unavailable_side in {side, "both"} else None
            assert getattr(change, f"{side}_unavailable_reason") == expected
        assert change.delta is None
        assert change.comparison_unavailable_reason == f"{unavailable_side}_unavailable"
    assert result.previous_attempt.speaking.recognized_word_count.comparable is True


@pytest.mark.parametrize("reason", get_args(TimingUnavailableReason))
@pytest.mark.parametrize("unavailable_side", ["before", "after"])
def test_all_timing_reasons_survive_both_metric_families(reason, unavailable_side):
    unavailable = measurement(
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason=reason,
        delivery_metrics=delivery(
            pause_count=None, total_pause_duration_seconds=None, longest_pause_seconds=None,
            unavailable_reason=reason,
        ),
    )
    before, after = (
        (unavailable, measurement()) if unavailable_side == "before"
        else (measurement(), unavailable)
    )
    supplied = comparison(before, after)
    result = context(after, supplied)
    for family, names in (
        (result.previous_attempt.speaking, SPEAKING_METRICS[-2:]),
        (result.previous_attempt.delivery, DELIVERY_METRICS),
    ):
        for name in names:
            change = getattr(family, name)
            assert getattr(change, unavailable_side) is None
            assert getattr(change, f"{unavailable_side}_unavailable_reason") == reason
            assert change.delta is None
            assert change.comparable is False
            assert change.comparison_unavailable_reason == f"{unavailable_side}_unavailable"
    assert result.measurement.model_dump() == after.model_dump()


@pytest.mark.parametrize(("before_legacy", "after_legacy"), [
    (True, True), (True, False), (False, True), (False, False),
])
def test_legacy_delivery_none_and_recorded_zero_remain_distinct(before_legacy, after_legacy):
    zero = delivery(pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0)
    before = measurement(delivery_metrics=None if before_legacy else zero)
    after = measurement(delivery_metrics=None if after_legacy else zero)
    supplied = comparison(before, after)
    result = context(after, supplied)
    assert result.measurement.delivery_metrics is (None if after_legacy else zero)
    facts = result.previous_attempt.delivery
    for side, legacy in (("before", before_legacy), ("after", after_legacy)):
        assert getattr(facts, f"{side}_version") == (None if legacy else zero.version)
        assert getattr(facts, f"{side}_source") == (None if legacy else zero.source)
        for name in DELIVERY_METRICS:
            change = getattr(facts, name)
            assert getattr(change, side) == (None if legacy else 0)
            assert getattr(change, f"{side}_unavailable_reason") == ("not_recorded" if legacy else None)
            assert change.delta == (None if before_legacy or after_legacy else 0)
    assert facts is supplied.delivery_comparison


def test_signed_exact_deltas_and_independent_provenance_are_copied_without_mutation():
    before = measurement(
        measurement_version=" speaking-historical-v9 ",
        delivery_metrics=delivery(version=" pause-historical-v7 ", source=" custom_delivery_source "),
    )
    after = measurement(
        measurement_version=before.measurement_version,
        recognized_word_count=13, um_count=0, uh_count=4,
        timed_utterance_span_seconds=12.123456789789,
        estimated_words_per_minute=48.891231199,
        delivery_metrics=delivery(
            version=before.delivery_metrics.version, source=before.delivery_metrics.source,
            pause_count=3, total_pause_duration_seconds=1.234567890789,
            longest_pause_seconds=0.734567889789,
        ),
    )
    supplied = comparison(before, after)
    originals = [value.model_dump(mode="json") for value in (before, after, supplied)]
    result = context(after, supplied)
    assert result.measurement is after
    assert result.previous_attempt.before_attempt_number == 2
    assert result.previous_attempt.after_attempt_number == 3
    assert result.previous_attempt.speaking is supplied.comparison
    assert result.previous_attempt.delivery is supplied.delivery_comparison
    for family, before_values, after_values, names in (
        (result.previous_attempt.speaking, before, after, SPEAKING_METRICS),
        (result.previous_attempt.delivery, before.delivery_metrics, after.delivery_metrics, DELIVERY_METRICS),
    ):
        for name in names:
            change = getattr(family, name)
            original_family = supplied.comparison if names == SPEAKING_METRICS else supplied.delivery_comparison
            assert change is getattr(original_family, name)
            assert change.before == getattr(before_values, name)
            assert change.after == getattr(after_values, name)
            assert change.delta == getattr(after_values, name) - getattr(before_values, name)
    assert result.previous_attempt.speaking.um_count.delta == -2
    assert result.previous_attempt.speaking.recognized_word_count.delta == 3
    assert result.previous_attempt.speaking.timed_utterance_span_seconds.delta > 0
    assert result.previous_attempt.delivery.longest_pause_seconds.delta < 0
    assert round(before.timed_utterance_span_seconds, 3) == round(after.timed_utterance_span_seconds, 3)
    assert round(before.delivery_metrics.longest_pause_seconds, 3) == round(after.delivery_metrics.longest_pause_seconds, 3)
    assert result.measurement.measurement_version == " speaking-historical-v9 "
    assert result.previous_attempt.delivery.before_version == " pause-historical-v7 "
    assert result.previous_attempt.delivery.after_version == " pause-historical-v7 "
    assert result.previous_attempt.delivery.before_source == " custom_delivery_source "
    assert result.previous_attempt.delivery.after_source == " custom_delivery_source "
    assert [value.model_dump(mode="json") for value in (before, after, supplied)] == originals


@pytest.mark.parametrize(("updates", "incompatible_family", "reason"), [
    ({"measurement_version": "speaking-metrics-v2"}, "speaking", "measurement_version_mismatch"),
    ({"measurement_source": "edited_answer"}, "speaking", "measurement_source_incompatible"),
    ({"delivery_metrics": delivery(version="pause-metrics-v2")}, "delivery", "measurement_version_mismatch"),
    ({"delivery_metrics": delivery(source="another_source")}, "delivery", "measurement_source_incompatible"),
])
def test_incompatibility_remains_independent_between_speaking_and_delivery(
    updates, incompatible_family, reason,
):
    before, after = measurement(), measurement(**updates)
    supplied = comparison(before, after)
    result = context(after, supplied)
    for family_name, names in (("speaking", SPEAKING_METRICS), ("delivery", DELIVERY_METRICS)):
        family = getattr(result.previous_attempt, family_name)
        for name in names:
            change = getattr(family, name)
            if family_name == incompatible_family:
                assert change.comparison_unavailable_reason == reason
                assert change.delta is None
                assert change.comparable is False
            else:
                assert change.comparison_unavailable_reason is None
                assert change.delta == 0
                assert change.comparable is True
    assert result.previous_attempt.speaking is supplied.comparison
    assert result.previous_attempt.delivery is supplied.delivery_comparison


def test_supplied_unusual_signed_unrounded_deltas_are_not_recomputed():
    before, after = measurement(), measurement(recognized_word_count=13)
    supplied = comparison(before, after)
    speaking_delta = -9.876543210987654
    delivery_delta = 7.654321098765432
    speaking_values = {name: getattr(supplied.comparison, name) for name in SPEAKING_METRICS}
    speaking_values["recognized_word_count"] = MetricChange(
        before=10, after=13, delta=speaking_delta,
        before_unavailable_reason=None, after_unavailable_reason=None,
        comparable=True, comparison_unavailable_reason=None,
    )
    speaking = ComparisonMetrics(**speaking_values)
    delivery_values = {
        name: getattr(supplied.delivery_comparison, name)
        for name in DeliveryComparison.model_fields
    }
    delivery_values["total_pause_duration_seconds"] = DeliveryMetricChange(
        before=0.123456789123, after=0.123456789789, delta=delivery_delta,
        before_unavailable_reason=None, after_unavailable_reason=None,
        comparable=True, comparison_unavailable_reason=None,
    )
    delivery_facts = DeliveryComparison(**delivery_values)
    supplied = comparison(before, after, comparison=speaking, delivery_comparison=delivery_facts)
    original = supplied.model_dump(mode="json")
    result = context(after, supplied)
    assert result.measurement is after
    assert result.previous_attempt.speaking is speaking
    assert result.previous_attempt.delivery is delivery_facts
    assert result.previous_attempt.speaking.recognized_word_count.delta == speaking_delta
    assert result.previous_attempt.delivery.total_pause_duration_seconds.delta == delivery_delta
    assert speaking_delta != 13 - 10
    assert delivery_delta != 0.123456789789 - 0.123456789123
    assert supplied.model_dump(mode="json") == original


def test_context_build_never_calls_authoritative_measurement_or_comparison_helpers(monkeypatch):
    from app import comparisons, delivery_metrics, diagnosis, speaking_metrics

    before, after = measurement(), measurement()
    supplied = comparison(before, after)
    originals = [value.model_dump(mode="json") for value in (before, after, supplied)]

    def fail_recalculation(*args, **kwargs):
        pytest.fail("Diagnosis construction must project the supplied metric facts.")

    for module, name in (
        (comparisons, "compare_measurements"),
        (comparisons, "compare_delivery_measurements"),
        (speaking_metrics, "measure_transcription"),
        (delivery_metrics, "measure_delivery"),
    ):
        monkeypatch.setattr(module, name, fail_recalculation)
        monkeypatch.setattr(diagnosis, name, fail_recalculation, raising=False)

    result = context(after, supplied)
    assert result.measurement is after
    assert result.previous_attempt.speaking is supplied.comparison
    assert result.previous_attempt.delivery is supplied.delivery_comparison
    assert [value.model_dump(mode="json") for value in (before, after, supplied)] == originals


@pytest.mark.parametrize(("field", "value"), [
    ("question_index", 0), ("question_index", 3),
    ("before_attempt", None), ("after_attempt", None),
    ("comparison", None), ("delivery_comparison", None),
])
def test_mismatched_question_or_incomplete_comparison_is_rejected_with_fixed_message(field, value):
    snapshot = measurement()
    supplied = comparison(snapshot, snapshot, **{field: value})
    original = supplied.model_dump(mode="json")
    with pytest.raises(ValueError) as error:
        context(snapshot, supplied)
    assert str(error.value) == INVALID_COMPARISON_MESSAGE
    assert supplied.model_dump(mode="json") == original


@pytest.mark.parametrize(("before_number", "after_number"), [
    (1, 3), (3, 3), (4, 3), (2, 2), (2, 4),
])
def test_comparison_must_end_at_current_attempt_and_start_at_immediate_predecessor(
    before_number, after_number,
):
    snapshot = measurement()
    supplied = comparison(
        snapshot, snapshot,
        before_attempt=compared_attempt(snapshot, before_number, IDENTITY_SENTINELS[1], IDENTITY_SENTINELS[3]),
        after_attempt=compared_attempt(snapshot, after_number, IDENTITY_SENTINELS[2], IDENTITY_SENTINELS[4]),
    )
    with pytest.raises(ValueError) as error:
        context(snapshot, supplied)
    assert str(error.value) == INVALID_COMPARISON_MESSAGE


def test_first_attempt_cannot_claim_a_previous_positive_attempt_number():
    snapshot = measurement()
    supplied = comparison(
        snapshot, snapshot,
        before_attempt=compared_attempt(snapshot, 1, IDENTITY_SENTINELS[1], IDENTITY_SENTINELS[3]),
        after_attempt=compared_attempt(snapshot, 1, IDENTITY_SENTINELS[2], IDENTITY_SENTINELS[4]),
    )
    with pytest.raises(ValueError) as error:
        context(snapshot, supplied, attempt_number=1)
    assert str(error.value) == INVALID_COMPARISON_MESSAGE


def test_question_and_answer_preserve_unicode_and_whitespace_exactly():
    question = " \tPourquoi café?\r\n決定 👩🏽‍💻 e\u0301 \u00a0"
    answer = "\n  Café / 咖啡 — e\u0301\t\r\n\u2003"
    result = context(question=question, answer=answer)
    assert result.question == question
    assert result.answer == answer
    assert result.model_dump(mode="json")["question"] == question
    assert result.model_dump(mode="json")["answer"] == answer


@pytest.mark.parametrize("field", ["before_attempt_number", "after_attempt_number"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, -1])
def test_previous_attempt_numbers_are_strict_positive_integers(field, value):
    values = previous_facts().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        PreviousAttemptFacts(**values)


@pytest.mark.parametrize(("field", "value"), [
    ("question_index", True), ("question_index", False), ("question_index", 0.0),
    ("question_index", "0"), ("question_index", -1),
    ("attempt_number", True), ("attempt_number", False), ("attempt_number", 1.0),
    ("attempt_number", "1"), ("attempt_number", 0), ("attempt_number", -1),
])
def test_context_indices_are_strict_and_respect_zero_vs_positive_bounds(field, value):
    values = context().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        DiagnosisContext(**values)
    with pytest.raises(ValidationError):
        context(**{field: value})
    snapshot = measurement()
    supplied = comparison(snapshot, snapshot)
    with pytest.raises(ValidationError):
        context(snapshot, supplied, **{field: value})


@pytest.mark.parametrize("field", ["question", "answer"])
@pytest.mark.parametrize("value", [b"text", 123, None])
def test_context_strings_are_strict(field, value):
    values = context().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        DiagnosisContext(**values)


@pytest.mark.parametrize("model_factory", [context, previous_facts])
@pytest.mark.parametrize("field", ["score", "session_id", "provider_words"])
def test_both_models_forbid_extra_identity_semantic_and_provider_fields(model_factory, field):
    model = model_factory()
    values = model.model_dump()
    values[field] = "extra"
    with pytest.raises(ValidationError) as error:
        type(model)(**values)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(("model_factory", "field", "value"), [
    (context, "answer", "changed"),
    (previous_facts, "before_attempt_number", 1),
])
def test_both_context_models_are_frozen(model_factory, field, value):
    model = model_factory()
    original = model.model_dump()
    with pytest.raises(ValidationError) as error:
        setattr(model, field, value)
    assert error.value.errors()[0]["type"] == "frozen_instance"
    assert model.model_dump() == original


def test_context_version_is_defaulted_and_rejects_other_versions():
    values = context().model_dump()
    values.pop("context_version")
    assert DiagnosisContext(**values).context_version == "diagnosis-context-v1"
    with pytest.raises(ValidationError):
        DiagnosisContext(**values, context_version="diagnosis-context-v2")


def test_serialized_context_is_exact_fact_allowlist_without_attempt_identity_or_enrichment():
    before, after = measurement(), measurement()
    supplied = comparison(before, after)
    originals = [value.model_dump(mode="json") for value in (before, after, supplied)]
    result = context(after, supplied)
    serialized = result.model_dump(mode="json")
    assert set(serialized) == {
        "context_version", "question", "answer", "question_index", "attempt_number",
        "measurement", "previous_attempt",
    }
    assert set(serialized["measurement"]) == {
        "measurement_version", "measurement_source", "recognized_word_count", "um_count",
        "uh_count", "filler_unavailable_reason", "timed_utterance_span_seconds",
        "estimated_words_per_minute", "timing_unavailable_reason", "delivery_metrics",
    }
    assert set(serialized["measurement"]["delivery_metrics"]) == {
        "version", "source", "pause_count", "total_pause_duration_seconds",
        "longest_pause_seconds", "unavailable_reason",
    }
    previous = serialized["previous_attempt"]
    assert set(previous) == {"before_attempt_number", "after_attempt_number", "speaking", "delivery"}
    assert set(previous["speaking"]) == set(SPEAKING_METRICS)
    assert set(previous["delivery"]) == {
        *DELIVERY_METRICS, "before_version", "after_version", "before_source", "after_source",
    }
    for name in SPEAKING_METRICS:
        assert set(previous["speaking"][name]) == CHANGE_FIELDS
    for name in DELIVERY_METRICS:
        assert set(previous["delivery"][name]) == CHANGE_FIELDS
    assert serialized["measurement"] == originals[1]
    assert previous["speaking"] == originals[2]["comparison"]
    assert previous["delivery"] == originals[2]["delivery_comparison"]
    forbidden_fields = {
        "id", "session_id", "attempt_id", "measurement_id", "before_attempt", "after_attempt",
        "score", "rating", "better", "worse", "recommendation", "feedback", "confidence", "quality",
        "provider", "provider_words", "provider_metadata", "model_metadata", "timestamps",
        "words", "word_timings", "pause_events", "audio",
        "transcript", "original_transcript",
    }

    def assert_no_forbidden_fields(value):
        if isinstance(value, dict):
            assert set(value).isdisjoint(forbidden_fields)
            for nested in value.values():
                assert_no_forbidden_fields(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_forbidden_fields(nested)

    assert_no_forbidden_fields(serialized)
    encoded = json.dumps(serialized, ensure_ascii=False)
    assert all(str(identity) not in encoded for identity in IDENTITY_SENTINELS)
    assert [value.model_dump(mode="json") for value in (before, after, supplied)] == originals

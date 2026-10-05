"""Neutral comparison of persisted original-transcription measurements.

The caller selects attempts and their exact linked measurements. This module
does not read answers, infer associations, recalculate metrics, or score changes.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.speaking_metrics import TimingUnavailableReason

MetricUnavailableReason = Literal["no_measurement", "unsupported_language"] | TimingUnavailableReason
ComparisonUnavailableReason = Literal[
    "measurement_version_mismatch", "measurement_source_incompatible",
    "before_unavailable", "after_unavailable", "both_unavailable",
]


class MeasurementSnapshot(BaseModel):
    """Only persisted scalar values; no transcript or provider timing arrays."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    measurement_version: str
    measurement_source: str
    recognized_word_count: Annotated[int, Field(ge=0)]
    um_count: Annotated[int, Field(ge=0)] | None
    uh_count: Annotated[int, Field(ge=0)] | None
    filler_unavailable_reason: Literal["unsupported_language"] | None
    timed_utterance_span_seconds: Annotated[float, Field(gt=0)] | None
    estimated_words_per_minute: Annotated[float, Field(gt=0)] | None
    timing_unavailable_reason: TimingUnavailableReason | None

    @field_validator("measurement_version")
    @classmethod
    def nonblank_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Measurement version must not be blank.")
        return value


class MetricChange(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    before: int | float | None
    after: int | float | None
    delta: int | float | None
    before_unavailable_reason: MetricUnavailableReason | None
    after_unavailable_reason: MetricUnavailableReason | None
    comparable: bool
    comparison_unavailable_reason: ComparisonUnavailableReason | None


class ComparisonMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recognized_word_count: MetricChange
    um_count: MetricChange
    uh_count: MetricChange
    timed_utterance_span_seconds: MetricChange
    estimated_words_per_minute: MetricChange


class ComparedAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    attempt_number: Annotated[int, Field(strict=True, gt=0)]
    measurement_id: UUID | None
    measurement_version: str | None
    measurement_source: str | None


class AttemptComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    question_index: Annotated[int, Field(strict=True, ge=0)]
    before_attempt: ComparedAttempt | None
    after_attempt: ComparedAttempt | None
    comparison: ComparisonMetrics | None


def compare_measurements(
    before: MeasurementSnapshot | None, after: MeasurementSnapshot | None,
) -> ComparisonMetrics:
    """Subtract unrounded stored values only for compatible, available pairs.

    Version mismatch takes precedence over source incompatibility when both
    measurements exist. Otherwise, unavailable sides determine the reason.
    Identical unknown sources remain incompatible: only original_transcription
    has the meaning required by this comparison contract.
    """
    compatibility_reason: ComparisonUnavailableReason | None = None
    if before is not None and after is not None:
        if before.measurement_version != after.measurement_version:
            compatibility_reason = "measurement_version_mismatch"
        elif (before.measurement_source != "original_transcription" or
              after.measurement_source != "original_transcription"):
            compatibility_reason = "measurement_source_incompatible"

    return ComparisonMetrics(
        recognized_word_count=_metric_change(
            before, after, "recognized_word_count", None, compatibility_reason,
        ),
        um_count=_metric_change(
            before, after, "um_count", "filler_unavailable_reason", compatibility_reason,
        ),
        uh_count=_metric_change(
            before, after, "uh_count", "filler_unavailable_reason", compatibility_reason,
        ),
        timed_utterance_span_seconds=_metric_change(
            before, after, "timed_utterance_span_seconds", "timing_unavailable_reason",
            compatibility_reason,
        ),
        estimated_words_per_minute=_metric_change(
            before, after, "estimated_words_per_minute", "timing_unavailable_reason",
            compatibility_reason,
        ),
    )


def _metric_change(
    before: MeasurementSnapshot | None, after: MeasurementSnapshot | None,
    value_field: str, reason_field: str | None,
    compatibility_reason: ComparisonUnavailableReason | None,
) -> MetricChange:
    before_value = getattr(before, value_field) if before is not None else None
    after_value = getattr(after, value_field) if after is not None else None
    before_reason = (
        "no_measurement" if before is None else getattr(before, reason_field)
        if reason_field is not None else None
    )
    after_reason = (
        "no_measurement" if after is None else getattr(after, reason_field)
        if reason_field is not None else None
    )
    unavailable = compatibility_reason
    if unavailable is None:
        if before_value is None and after_value is None:
            unavailable = "both_unavailable"
        elif before_value is None:
            unavailable = "before_unavailable"
        elif after_value is None:
            unavailable = "after_unavailable"
    return MetricChange(
        before=before_value, after=after_value,
        delta=after_value - before_value if unavailable is None else None,
        before_unavailable_reason=before_reason, after_unavailable_reason=after_reason,
        comparable=unavailable is None, comparison_unavailable_reason=unavailable,
    )

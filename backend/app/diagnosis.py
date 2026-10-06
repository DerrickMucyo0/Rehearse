"""Pure context projection of authoritative facts for future semantic diagnosis.

Measurements and comparisons are supplied by the caller, never recalculated here.
This module performs no judgment, orchestration, persistence, or external access.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.comparisons import (
    AttemptComparison, ComparisonMetrics, DeliveryComparison, MeasurementSnapshot,
)

DIAGNOSIS_CONTEXT_VERSION = "diagnosis-context-v1"


class PreviousAttemptFacts(BaseModel):
    """An adjacent attempt pair's existing comparisons, without identities."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    before_attempt_number: Annotated[int, Field(strict=True, gt=0)]
    after_attempt_number: Annotated[int, Field(strict=True, gt=0)]
    speaking: ComparisonMetrics
    delivery: DeliveryComparison


class DiagnosisContext(BaseModel):
    """Unchanged answer text and objective facts; no semantic assessment."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    context_version: Literal["diagnosis-context-v1"] = DIAGNOSIS_CONTEXT_VERSION
    question: str
    answer: str
    question_index: Annotated[int, Field(strict=True, ge=0)]
    attempt_number: Annotated[int, Field(strict=True, gt=0)]
    measurement: MeasurementSnapshot | None
    previous_attempt: PreviousAttemptFacts | None


def build_diagnosis_context(
    *,
    question: str,
    answer: str,
    question_index: int,
    attempt_number: int,
    measurement: MeasurementSnapshot | None,
    comparison: AttemptComparison | None = None,
) -> DiagnosisContext:
    """Project supplied facts, requiring an immediately preceding comparison."""
    # Validate strict counters before equality or adjacency checks (True == 1).
    context = DiagnosisContext(
        question=question, answer=answer, question_index=question_index,
        attempt_number=attempt_number, measurement=measurement, previous_attempt=None,
    )
    if comparison is None:
        return context
    if (
        not isinstance(comparison, AttemptComparison)
        or comparison.question_index != context.question_index
        or comparison.before_attempt is None
        or comparison.after_attempt is None
        or comparison.comparison is None
        or comparison.delivery_comparison is None
        or comparison.after_attempt.attempt_number != context.attempt_number
        or comparison.before_attempt.attempt_number != context.attempt_number - 1
    ):
        raise ValueError("Comparison must describe the immediately previous attempt for this question.")
    previous = PreviousAttemptFacts(
        before_attempt_number=comparison.before_attempt.attempt_number,
        after_attempt_number=comparison.after_attempt.attempt_number,
        speaking=comparison.comparison, delivery=comparison.delivery_comparison,
    )
    # Both objects are already validated; copy the context without mutating inputs
    # or reconstructing the authoritative measurement/comparison objects.
    return context.model_copy(update={"previous_attempt": previous})

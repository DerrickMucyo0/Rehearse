"""Pure checks of supplied diagnoses against explicitly curated expectations.

Context is provenance only. No expectations are inferred from context or feedback
text, and no universal interview-quality policy is applied here.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis import (
    AddressedQuestion, NextFocus, SemanticDiagnosis, StructureAssessment,
)

EVAL_EXPECTATION_VERSION = "semantic-eval-expectation-v1"


class SemanticDiagnosisExpectation(BaseModel):
    """Accepted semantic alternatives and collection-emptiness requirements."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    expectation_version: Literal["semantic-eval-expectation-v1"] = EVAL_EXPECTATION_VERSION
    allowed_addressed_question: Annotated[frozenset[AddressedQuestion], Field(min_length=1)] | None = None
    allowed_structure: Annotated[frozenset[StructureAssessment], Field(min_length=1)] | None = None
    allowed_next_focus: Annotated[frozenset[NextFocus], Field(min_length=1)] | None = None
    strengths_requirement: Literal["any", "empty", "nonempty"] = "any"
    missing_information_requirement: Literal["any", "empty", "nonempty"] = "any"


SemanticEvalViolationCode = Literal[
    "addressed_question_not_allowed",
    "structure_not_allowed",
    "next_focus_not_allowed",
    "strengths_must_be_empty",
    "strengths_must_be_nonempty",
    "missing_information_must_be_empty",
    "missing_information_must_be_nonempty",
]


class SemanticEvalViolation(BaseModel):
    """One failed expectation dimension, without text or scoring metadata."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: SemanticEvalViolationCode


def evaluate_semantic_diagnosis(
    *,
    context: DiagnosisContext,
    diagnosis: SemanticDiagnosis,
    expectation: SemanticDiagnosisExpectation,
) -> tuple[SemanticEvalViolation, ...]:
    """Return all explicit expectation violations in a stable dimension order."""
    if not isinstance(context, DiagnosisContext):
        raise TypeError("Semantic evaluation context must be a DiagnosisContext instance.")
    if not isinstance(diagnosis, SemanticDiagnosis):
        raise TypeError("Semantic evaluation diagnosis must be a SemanticDiagnosis instance.")
    if not isinstance(expectation, SemanticDiagnosisExpectation):
        raise TypeError("Semantic evaluation expectation must be a SemanticDiagnosisExpectation instance.")

    violations: list[SemanticEvalViolation] = []
    if (
        expectation.allowed_addressed_question is not None
        and diagnosis.addressed_question not in expectation.allowed_addressed_question
    ):
        violations.append(SemanticEvalViolation(code="addressed_question_not_allowed"))
    if (
        expectation.allowed_structure is not None
        and diagnosis.structure not in expectation.allowed_structure
    ):
        violations.append(SemanticEvalViolation(code="structure_not_allowed"))
    if (
        expectation.allowed_next_focus is not None
        and diagnosis.next_focus not in expectation.allowed_next_focus
    ):
        violations.append(SemanticEvalViolation(code="next_focus_not_allowed"))
    if expectation.strengths_requirement == "empty" and diagnosis.strengths:
        violations.append(SemanticEvalViolation(code="strengths_must_be_empty"))
    elif expectation.strengths_requirement == "nonempty" and not diagnosis.strengths:
        violations.append(SemanticEvalViolation(code="strengths_must_be_nonempty"))
    if expectation.missing_information_requirement == "empty" and diagnosis.missing_information:
        violations.append(SemanticEvalViolation(code="missing_information_must_be_empty"))
    elif expectation.missing_information_requirement == "nonempty" and not diagnosis.missing_information:
        violations.append(SemanticEvalViolation(code="missing_information_must_be_nonempty"))
    return tuple(violations)

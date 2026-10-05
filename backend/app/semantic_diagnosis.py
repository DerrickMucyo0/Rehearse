"""Bounded semantic output contract for a future reasoning-model adapter.

This module only validates supplied output. It generates no feedback and has no
dependency on measurements, context projection, providers, or orchestration.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

SEMANTIC_DIAGNOSIS_VERSION = "semantic-diagnosis-v1"

AddressedQuestion = Literal["yes", "partially", "no"]
StructureAssessment = Literal["clear", "mixed", "unclear", "insufficient_content"]
NextFocus = Literal[
    "answer_the_question", "specificity", "supporting_detail", "structure",
    "completeness", "conciseness", "maintain_strengths",
]


def _validate_feedback_text(value: str) -> str:
    """Reject blank or padded text without altering any accepted content."""
    if not value.strip() or value != value.strip():
        raise ValueError("Feedback text must be nonblank and have no surrounding whitespace.")
    return value


FeedbackText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=600),
    AfterValidator(_validate_feedback_text),
]
FeedbackItems = Annotated[tuple[FeedbackText, ...], Field(min_length=0, max_length=3)]


class SemanticDiagnosis(BaseModel):
    """The complete semantic response shape, separate from objective facts."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    diagnosis_version: Literal["semantic-diagnosis-v1"] = SEMANTIC_DIAGNOSIS_VERSION
    addressed_question: AddressedQuestion
    addressed_question_reason: FeedbackText
    strengths: FeedbackItems
    missing_information: FeedbackItems
    structure: StructureAssessment
    structure_feedback: FeedbackText
    next_focus: NextFocus
    next_focus_reason: FeedbackText
    retry_instruction: FeedbackText

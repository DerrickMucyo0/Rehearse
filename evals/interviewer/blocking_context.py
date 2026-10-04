"""Development-only blocking-context contract; no session integration."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.reasoning import Action, ShortText, parse_json_content

# Reuse the closed schema diagnostics without changing earlier contracts.
from evals.interviewer.assessment_independent import (
    InvalidAssessment, schema_diagnostic)


class BlockingContextAssessment(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    understandable_relevant: bool
    blocking_context_gap: bool | None
    unresolved_reasoning_issue: bool | None
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                           min_length=1, max_length=300)]
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def determinate_when_understood(self):
        if self.understandable_relevant and (
                self.blocking_context_gap is None or self.unresolved_reasoning_issue is None):
            raise PydanticCustomError('invalid_assessment_combination', 'Invalid assessment combination')
        if not self.understandable_relevant and (
                self.blocking_context_gap is not None or self.unresolved_reasoning_issue is not None):
            raise PydanticCustomError('invalid_assessment_combination', 'Invalid assessment combination')
        return self

    @model_validator(mode='after')
    def consistent_prompt(self):
        move_on = (self.understandable_relevant and self.blocking_context_gap is False
                   and self.unresolved_reasoning_issue is False)
        if move_on != (self.next_prompt is None):
            raise PydanticCustomError('assessment_prompt_inconsistency', 'Inconsistent assessment prompt')
        return self


def validate_assessment(value) -> BlockingContextAssessment:
    try:
        return BlockingContextAssessment.model_validate(value)
    except ValidationError as error:
        raise InvalidAssessment(schema_diagnostic(error)) from None


def map_assessment(assessment: BlockingContextAssessment) -> Action:
    """Only validated boolean precedence; no interpretation of user/model text."""
    assessment = validate_assessment(assessment)
    if not assessment.understandable_relevant:
        return 'CLARIFY'
    if assessment.blocking_context_gap:
        return 'FOLLOW_UP'
    if assessment.unresolved_reasoning_issue:
        return 'CHALLENGE'
    return 'MOVE_ON'


def parse_assessment(value: str) -> BlockingContextAssessment:
    decoded = parse_json_content(value)
    return validate_assessment(decoded)

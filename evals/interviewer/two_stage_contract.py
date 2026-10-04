"""Strict, development-only semantic gates. No provider or session operations."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.reasoning import Decision, ShortText, parse_json_content
from evals.interviewer.assessment_independent import InvalidAssessment, schema_diagnostic

StageId = Literal['stage_1', 'stage_2']
Route = Literal['CLARIFY', 'FOLLOW_UP', 'CONTINUE_TO_STAGE_2']
Reason = Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                        min_length=1, max_length=300)]


class Stage1(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    understandable_relevant: bool
    blocking_context_gap: bool | None
    reason: Reason
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def consistent(self):
        if (self.understandable_relevant and self.blocking_context_gap is None) or (
                not self.understandable_relevant and self.blocking_context_gap is not None):
            raise PydanticCustomError('invalid_assessment_combination', 'Invalid gate combination')
        continues = self.understandable_relevant and self.blocking_context_gap is False
        if continues != (self.next_prompt is None):
            raise PydanticCustomError('assessment_prompt_inconsistency', 'Invalid gate prompt')
        return self


class Stage2(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    unresolved_reasoning_issue: bool
    reason: Reason
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def consistent(self):
        if self.unresolved_reasoning_issue != (self.next_prompt is not None):
            raise PydanticCustomError('assessment_prompt_inconsistency', 'Invalid reasoning prompt')
        return self


def _validate(model, value):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise InvalidAssessment(schema_diagnostic(error)) from None


def validate_stage1(value) -> Stage1:
    return _validate(Stage1, value)


def validate_stage2(value) -> Stage2:
    return _validate(Stage2, value)


def parse_stage1(content: str) -> Stage1:
    return validate_stage1(parse_json_content(content))


def parse_stage2(content: str) -> Stage2:
    return validate_stage2(parse_json_content(content))


def route_stage1(value: Stage1) -> Route:
    value = validate_stage1(value)
    if not value.understandable_relevant:
        return 'CLARIFY'
    if value.blocking_context_gap:
        return 'FOLLOW_UP'
    return 'CONTINUE_TO_STAGE_2'


def terminal_stage1(value: Stage1) -> Decision:
    value = validate_stage1(value)
    route = route_stage1(value)
    if route == 'CONTINUE_TO_STAGE_2':
        raise ValueError('Stage 2 is required')
    return Decision(action=route, reason=value.reason, next_prompt=value.next_prompt)


def map_stage2(value: Stage2) -> Decision:
    value = validate_stage2(value)
    return Decision(action='CHALLENGE' if value.unresolved_reasoning_issue else 'MOVE_ON',
                    reason=value.reason, next_prompt=value.next_prompt)

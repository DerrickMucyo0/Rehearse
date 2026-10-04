"""Strict development-only Stage 2 recommendation; historical contracts stay frozen."""
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.reasoning import Decision, ShortText, parse_json_content
from evals.interviewer.assessment_independent import InvalidAssessment, schema_diagnostic
from evals.interviewer.two_stage_contract import Reason


class ChallengeStage2(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    challenge_warranted: bool
    reason: Reason
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def consistent(self):
        if self.challenge_warranted != (self.next_prompt is not None):
            raise PydanticCustomError('assessment_prompt_inconsistency',
                                     'Invalid challenge prompt')
        return self


def validate_stage2(value) -> ChallengeStage2:
    try:
        return ChallengeStage2.model_validate(value)
    except ValidationError as error:
        raise InvalidAssessment(schema_diagnostic(error)) from None


def parse_stage2(content: str) -> ChallengeStage2:
    return validate_stage2(parse_json_content(content))


def map_stage2(value: ChallengeStage2) -> Decision:
    value = validate_stage2(value)
    return Decision(action='CHALLENGE' if value.challenge_warranted else 'MOVE_ON',
                    reason=value.reason, next_prompt=value.next_prompt)

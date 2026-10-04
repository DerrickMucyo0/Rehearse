"""Evaluation-only independent assessment v2; production and v1 are unchanged."""
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.reasoning import (Action, InvalidDecision,
                          ShortText, parse_json_content)

SchemaReason = Literal['missing_required_field', 'forbidden_extra_field',
    'strict_type_violation', 'text_bound_violation', 'invalid_assessment_combination',
    'assessment_prompt_inconsistency', 'other_schema_violation']
# First matching category wins; priority is stable and contains no field values.
SCHEMA_PRIORITY = get_args(SchemaReason)


class InvalidAssessment(InvalidDecision):
    def __init__(self, schema_reason: SchemaReason):
        if schema_reason not in SCHEMA_PRIORITY:
            raise ValueError('Unknown schema diagnostic')
        super().__init__(invalid_reason='schema_validation')
        self.schema_reason = schema_reason


def schema_diagnostic(error: ValidationError) -> SchemaReason:
    """Inspect only closed error types; never retain/serialize error dictionaries."""
    found = set()
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        kind = item['type']
        if kind == 'missing':
            code = 'missing_required_field'
        elif kind == 'extra_forbidden':
            code = 'forbidden_extra_field'
        elif kind in ('bool_type', 'string_type'):
            code = 'strict_type_violation'
        elif kind in ('string_too_short', 'string_too_long'):
            code = 'text_bound_violation'
        elif kind in ('invalid_assessment_combination', 'assessment_prompt_inconsistency'):
            code = kind
        else:
            code = 'other_schema_violation'
        found.add(code)
    return next(code for code in SCHEMA_PRIORITY if code in found) if found else 'other_schema_violation'


class IndependentAssessment(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    understandable_relevant: bool
    essential_descriptive_gap: bool | None
    unresolved_reasoning_issue: bool | None
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                           min_length=1, max_length=300)]
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def determinate_when_understood(self):
        if self.understandable_relevant and (
                self.essential_descriptive_gap is None or self.unresolved_reasoning_issue is None):
            raise PydanticCustomError('invalid_assessment_combination', 'Invalid assessment combination')
        return self

    @model_validator(mode='after')
    def consistent_prompt(self):
        move_on = (self.understandable_relevant and self.essential_descriptive_gap is False
                   and self.unresolved_reasoning_issue is False)
        if move_on != (self.next_prompt is None):
            raise PydanticCustomError('assessment_prompt_inconsistency', 'Inconsistent assessment prompt')
        return self


def validate_assessment(value) -> IndependentAssessment:
    try:
        return IndependentAssessment.model_validate(value)
    except ValidationError as error:
        raise InvalidAssessment(schema_diagnostic(error)) from None


def map_independent_assessment(assessment: IndependentAssessment) -> Action:
    """Only validated boolean precedence; no interpretation of user/model text."""
    assessment = validate_assessment(assessment)
    if not assessment.understandable_relevant:
        return 'CLARIFY'
    if assessment.essential_descriptive_gap:
        return 'FOLLOW_UP'
    if assessment.unresolved_reasoning_issue:
        return 'CHALLENGE'
    return 'MOVE_ON'


def parse_independent_assessment(value: str) -> IndependentAssessment:
    decoded = parse_json_content(value)
    return validate_assessment(decoded)

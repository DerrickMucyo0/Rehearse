"""Evaluation-only four-field assessment; no question generation or session wiring."""
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.reasoning import (Action, InvalidDecision,
                          parse_json_content)

SchemaReason = Literal['missing_required_field', 'forbidden_extra_field',
    'strict_type_violation', 'text_bound_violation', 'invalid_assessment_combination',
    'other_schema_violation']
# First matching category wins; priority is stable and contains no field values.
SCHEMA_PRIORITY = get_args(SchemaReason)


class InvalidAssessmentOnly(InvalidDecision):
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
        elif kind == 'invalid_assessment_combination':
            code = kind
        else:
            code = 'other_schema_violation'
        found.add(code)
    return next(code for code in SCHEMA_PRIORITY if code in found) if found else 'other_schema_violation'


class AssessmentOnly(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    understandable_relevant: bool
    essential_descriptive_gap: bool | None
    unresolved_reasoning_issue: bool | None
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                           min_length=1, max_length=300)]

    @model_validator(mode='after')
    def determinate_when_understood(self):
        if self.understandable_relevant and (
                self.essential_descriptive_gap is None or self.unresolved_reasoning_issue is None):
            raise PydanticCustomError('invalid_assessment_combination', 'Invalid assessment combination')
        return self



def validate_assessment(value) -> AssessmentOnly:
    try:
        return AssessmentOnly.model_validate(value)
    except ValidationError as error:
        raise InvalidAssessmentOnly(schema_diagnostic(error)) from None


def map_assessment_only(assessment: AssessmentOnly) -> Action:
    """Only validated boolean precedence; no interpretation of user/model text."""
    assessment = validate_assessment(assessment)
    if not assessment.understandable_relevant:
        return 'CLARIFY'
    if assessment.essential_descriptive_gap:
        return 'FOLLOW_UP'
    if assessment.unresolved_reasoning_issue:
        return 'CHALLENGE'
    return 'MOVE_ON'


def parse_assessment_only(value: str) -> AssessmentOnly:
    decoded = parse_json_content(value)
    return validate_assessment(decoded)

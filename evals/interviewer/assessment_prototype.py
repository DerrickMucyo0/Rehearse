"""Offline-only assessment contract; no provider, dataset loading or session wiring.

Nemotron would own all semantic assessments. Rehearse only validates branches
and maps them. Derived development expectations are rubric implications, NOT
independently human-reviewed ground truth.
"""
from collections.abc import Iterable
from types import MappingProxyType
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.reasoning import Action, ShortText

# No answer text or other semantic inputs participate in this lookup.
BRANCH_ACTIONS = MappingProxyType({
    (False, None, None): 'CLARIFY',
    (True, True, None): 'FOLLOW_UP',
    (True, False, True): 'CHALLENGE',
    (True, False, False): 'MOVE_ON',
})


class AssessmentBranch(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True,
                              revalidate_instances='always')
    # Understandability/relevance does not imply satisfactory justification.
    understandable_relevant: bool
    # Essential missing facts about events, contribution or results.
    essential_descriptive_gap: bool | None
    # Existing assertion/decision merits justification, evidence, assumption,
    # consequence, cost or alternative examination. Missing support is not
    # automatically a descriptive gap.
    unresolved_reasoning_issue: bool | None

    def branch_key(self):
        return (self.understandable_relevant, self.essential_descriptive_gap,
                self.unresolved_reasoning_issue)

    @model_validator(mode='after')
    def valid_branch(self):
        if self.branch_key() not in BRANCH_ACTIONS:
            raise ValueError('Invalid assessment branch')
        return self


class Assessment(AssessmentBranch):
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                           min_length=1, max_length=300)]
    next_prompt: ShortText | None

    @model_validator(mode='after')
    def consistent_prompt(self):
        if (BRANCH_ACTIONS[self.branch_key()] == 'MOVE_ON') != (self.next_prompt is None):
            raise ValueError('Inconsistent assessment and prompt')
        return self


def map_assessment(assessment: Assessment) -> Action:
    """Pure mapping of a validated branch; never interprets reason/prompt text."""
    validated = Assessment.model_validate(assessment)
    return BRANCH_ACTIONS[validated.branch_key()]


class DevelopmentLabel(Protocol):
    id: str
    split: str
    expected_action: Action


class DerivedExpectation(AssessmentBranch):
    case_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    annotation_status: Literal['rubric_derived_not_human_reviewed'] = 'rubric_derived_not_human_reviewed'


def derive_development_expectations(cases: Iterable[DevelopmentLabel]) -> tuple[DerivedExpectation, ...]:
    """Accept supplied development labels only; never load/annotate held-out data.

    Reject the entire collection before producing expectations if any split is
    not development. This is not an independent assessment of answer semantics.
    """
    cases = tuple(cases)
    if any(case.split != 'development' for case in cases):
        raise ValueError('Development labels required')
    if len({case.id for case in cases}) != len(cases):
        raise ValueError('Duplicate development IDs')
    branches = {action: key for key, action in BRANCH_ACTIONS.items()}
    results = []
    for case in cases:
        if case.expected_action not in branches:
            raise ValueError('Invalid locked action')
        understood, gap, issue = branches[case.expected_action]
        results.append(DerivedExpectation(case_id=case.id,
            understandable_relevant=understood, essential_descriptive_gap=gap,
            unresolved_reasoning_issue=issue))
    return tuple(results)

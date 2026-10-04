"""Offline contract tests with synthetic assessments, not model-intelligence mocks."""
import itertools
import json
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.reasoning import InvalidDecision, ProviderFailure, ReasoningFailed
# Follow the existing standalone-evaluator test loading convention.
spec = importlib.util.spec_from_file_location('assessment_prototype',
    Path(__file__).parents[1] / 'evals/interviewer/assessment_prototype.py')
prototype = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prototype)
Assessment = prototype.Assessment
BRANCH_ACTIONS = prototype.BRANCH_ACTIONS
derive_development_expectations = prototype.derive_development_expectations
map_assessment = prototype.map_assessment

BRANCHES = [
    ((False, None, None), 'CLARIFY'),
    ((True, True, None), 'FOLLOW_UP'),
    ((True, False, True), 'CHALLENGE'),
    ((True, False, False), 'MOVE_ON'),
]


def payload(key=(True, False, True), **changes):
    result = dict(zip(('understandable_relevant', 'essential_descriptive_gap',
                       'unresolved_reasoning_issue'), key))
    result.update(reason='Short explanation.',
                  next_prompt=None if key == (True, False, False) else 'Focused question?')
    result.update(changes)
    return result


@pytest.mark.parametrize('key,action', BRANCHES)
def test_valid_branches_and_mapping(key, action):
    assert map_assessment(Assessment(**payload(key))) == action


@pytest.mark.parametrize('key', [key for key in itertools.product((False, True, None), repeat=3)
                               if key not in dict(BRANCHES)])
def test_every_other_boolean_null_combination_rejected(key):
    with pytest.raises(ValidationError):
        Assessment(**payload(key))


@pytest.mark.parametrize('field', ['understandable_relevant', 'essential_descriptive_gap',
                                  'unresolved_reasoning_issue'])
@pytest.mark.parametrize('value', [0, 1, 'true', 'false', 'null'])
def test_boolean_coercion_rejected(field, value):
    with pytest.raises(ValidationError):
        Assessment(**payload(**{field: value}))


@pytest.mark.parametrize('field', list(payload()))
def test_all_fields_required(field):
    value = payload()
    del value[field]
    with pytest.raises(ValidationError):
        Assessment(**value)


@pytest.mark.parametrize('field', ['reasoning_content', 'hidden_reasoning', 'action', 'extra'])
def test_extra_fields_rejected(field):
    with pytest.raises(ValidationError):
        Assessment(**payload(**{field: 'HIDDEN_TRACE_MARKER'}))


@pytest.mark.parametrize('field,limit', [('reason', 300), ('next_prompt', 500)])
def test_text_trimming_and_boundaries(field, limit):
    assessment = Assessment(**payload(**{field: '  ' + 'x' * limit + '  '}))
    assert getattr(assessment, field) == 'x' * limit
    for value in ('', '  ', 'x' * (limit + 1), 123, True):
        with pytest.raises(ValidationError):
            Assessment(**payload(**{field: value}))


@pytest.mark.parametrize('key,action', BRANCHES)
def test_action_prompt_consistency(key, action):
    bad_prompt = 'Question?' if action == 'MOVE_ON' else None
    with pytest.raises(ValidationError):
        Assessment(**payload(key, next_prompt=bad_prompt))


@pytest.mark.parametrize('key,action', BRANCHES)
def test_mapping_independent_of_text(key, action):
    for text in ('clarify follow_up challenge move_on', 'No semantic clues.', 'Any arbitrary text.'):
        assessment = Assessment(**payload(key, reason=text,
            next_prompt=None if action == 'MOVE_ON' else text))
        assert map_assessment(assessment) == action
    assert dict(BRANCH_ACTIONS) == dict(BRANCHES)


def test_mapping_revalidates_constructed_and_copied_instances():
    invalid = Assessment.model_construct(**payload((False, True, True)))
    with pytest.raises(ValidationError):
        map_assessment(invalid)
    invalid = Assessment(**payload()).model_copy(update={'next_prompt': None})
    with pytest.raises(ValidationError):
        map_assessment(invalid)


def test_serialization_has_only_application_contract_fields():
    assessment = Assessment(**payload())
    assert set(json.loads(assessment.model_dump_json())) == set(payload())
    assert 'HIDDEN_TRACE_MARKER' not in assessment.model_dump_json()
    with pytest.raises(ValidationError):
        assessment.reason = 'Changed'


@dataclass(frozen=True)
class Label:
    id: str
    split: str
    expected_action: str


@pytest.mark.parametrize('key,action', BRANCHES)
def test_development_label_implies_branch_without_answer_inspection(key, action):
    result, = derive_development_expectations([Label('synthetic-label', 'development', action)])
    assert result.branch_key() == key
    assert result.annotation_status == 'rubric_derived_not_human_reviewed'
    assert set(result.model_dump()) == {'case_id', 'annotation_status',
        'understandable_relevant', 'essential_descriptive_gap', 'unresolved_reasoning_issue'}


@pytest.mark.parametrize('split', ['held_out', 'unknown'])
def test_nondevelopment_collection_rejected(split):
    with pytest.raises(ValueError, match='Development labels required'):
        derive_development_expectations([
            Label('dev-label', 'development', 'CLARIFY'), Label('other-label', split, 'MOVE_ON')])


def test_invalid_label_and_duplicate_ids_rejected():
    with pytest.raises(ValueError, match='Invalid locked action'):
        derive_development_expectations([Label('label', 'development', 'OTHER')])
    with pytest.raises(ValueError, match='Duplicate development IDs'):
        derive_development_expectations([Label('label', 'development', 'CLARIFY')] * 2)
    assert derive_development_expectations([]) == ()


def test_sanitized_failure_metadata_unchanged():
    failure = ReasoningFailed(failure=ProviderFailure(failure_kind='http_status', http_status=429))
    invalid = InvalidDecision(invalid_reason='json_syntax')
    assert failure.args == invalid.args == ()
    assert failure.failure.model_dump() == {'failure_kind': 'http_status', 'http_status': 429}
    assert invalid.invalid_reason == 'json_syntax'
    assert invalid.failure is None

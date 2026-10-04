"""Isolated assessment evaluation contracts; all provider responses are mocked."""
import asyncio
import ast
import itertools
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from evals.interviewer import assessment_live as live
from evals.interviewer.assessment_prototype import Assessment, BRANCH_ACTIONS
from evals.interviewer.evaluate import Case
from app.reasoning import InvalidDecision


def content(key=(True, False, True)):
    return dict(zip(live.SEMANTIC_FIELDS, key), reason='PRIVATE_REASON',
                next_prompt=None if key == (True, False, False) else 'PRIVATE_PROMPT')


def case(action='CHALLENGE', split='development'):
    return Case(id='synthetic-001', split=split, category='unsupported_claim',
        question='PRIVATE_QUESTION', current_prompt='PRIVATE_CURRENT', prior_turns=[],
        answer='PRIVATE_ANSWER', expected_action=action, label_reason='Private label')


def response(value, **extra):
    return {'choices': [{'finish_reason': 'stop', 'message':
        {'role': 'assistant', 'content': value, **extra}}]}


def report(monkeypatch, value=None, status=200, error=None, envelope=None):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    def handler(request):
        body = json.loads(request.content)
        assert body['model'] == live.MODEL
        assert body['messages'][0]['content'] == live.ASSESSMENT_INSTRUCTIONS
        for name, expected in live.asdict(live.ASSESSMENT_CONFIG).items():
            assert body[name] == expected
        if error:
            raise error
        return httpx.Response(status, json=envelope if envelope is not None else response(value,
            reasoning_content='HIDDEN_TRACE_MARKER'), headers={'Authorization': 'PRIVATE_HEADER'})
    return asyncio.run(live.assessment_report([case()],
        live.AssessmentService(httpx.MockTransport(handler))))


def test_prompt_contract_order_and_no_action_names():
    policy = ' '.join(live.ASSESSMENT_INSTRUCTIONS.split())
    assert 'Do not output an action field or an action label' in policy
    for action in ('CLARIFY', 'FOLLOW_UP', 'CHALLENGE', 'MOVE_ON'):
        assert action not in policy
    assert [policy.index(f'{i}.') for i in range(1, 5)] == sorted(policy.index(f'{i}.') for i in range(1, 5))
    assert 'remaining substantive answer together with relevant prior_turns' in policy
    assert 'current_prompt is the immediate question' in policy
    assert 'not automatically a descriptive gap' in policy


@pytest.mark.parametrize('key,action', list(BRANCH_ACTIONS.items()))
def test_strict_contract_maps_branch(key, action):
    assessment = live.parse_assessment(json.dumps(content(key)))
    assert isinstance(assessment, Assessment)
    assert live.map_assessment(assessment) == action


@pytest.mark.parametrize('key', [k for k in itertools.product((True, False, None), repeat=3)
                               if k not in BRANCH_ACTIONS])
def test_invalid_branches(key):
    with pytest.raises(InvalidDecision) as caught:
        live.parse_assessment(json.dumps(content(key)))
    assert caught.value.invalid_reason == 'schema_validation'


@pytest.mark.parametrize('value,code', [('{private', 'json_syntax'),
    ('{"x":1,"x":2}', 'duplicate_json_key'), ('NaN', 'non_json_constant'),
    (None, 'content_type'), ('x'*8193, 'content_size')])
def test_invalid_content_codes(value, code):
    with pytest.raises(InvalidDecision) as caught:
        live.parse_assessment(value)
    assert caught.value.invalid_reason == code
    assert caught.value.args == ()


def test_valid_report_excludes_text_and_hidden_trace(monkeypatch):
    result = report(monkeypatch, json.dumps(content()))
    row = result['outcomes'][0]
    assert row['predicted'] == 'CHALLENGE'
    assert row['assessment_valid'] and row['mapped_action_correct']
    assert result['assessment_metrics']['unresolved_reasoning_issue']['accuracy'] == 1
    assert 'PRIVATE' not in json.dumps(result)
    assert 'HIDDEN_TRACE_MARKER' not in json.dumps(result)


@pytest.mark.parametrize('kwargs,error,code,kind,status', [
    ({'value':'PRIVATE_INVALID'}, 'invalid_output', 'json_syntax', None, None),
    ({'envelope':{'choices':[{'message':[]}]}}, 'invalid_output','response_envelope',None,None),
    ({'status':429}, 'provider_error',None,'http_status',429),
    ({'error':httpx.ConnectError('PRIVATE_EXCEPTION')}, 'provider_error',None,'transport',None),
    ({'error':httpx.ReadTimeout('PRIVATE_EXCEPTION')}, 'timeout',None,None,None),
])
def test_failure_diagnostics_sanitized(monkeypatch, kwargs, error, code, kind, status):
    result = report(monkeypatch, **kwargs)
    row = result['outcomes'][0]
    assert (row['error'], row['invalid_reason'], row['failure_kind'], row['http_status']) == (error,code,kind,status)
    assert not row['assessment_valid'] and row['predicted'] is None
    assert result['assessment_metrics']['understandable_relevant'] == {'correct':0,'applicable':1,'accuracy':0}
    assert 'PRIVATE' not in json.dumps(result)


def test_expected_applicability_not_predicted_branch(monkeypatch):
    result = report(monkeypatch, json.dumps(content((False,None,None))))
    assert result['assessment_metrics']['essential_descriptive_gap']['applicable'] == 1
    assert result['assessment_metrics']['unresolved_reasoning_issue'] == {'correct':0,'applicable':1,'accuracy':0}


def test_heldout_rejected_before_service_request():
    class Forbidden:
        async def decide(self, context):
            pytest.fail('No request permitted')
    with pytest.raises(ValueError):
        asyncio.run(live.assessment_report([case(split='held_out')], Forbidden()))


def test_selection_heldout_and_unknown_rejected(monkeypatch, tmp_path):
    dataset = tmp_path/'synthetic.jsonl'
    dataset.write_text(case().model_dump_json()+'\n'+case(split='held_out').model_copy(update={'id':'synthetic-002'}).model_dump_json())
    monkeypatch.setattr(live, 'DATASET', dataset)
    assert live.select_development_cases(['synthetic-001'])[0].split == 'development'
    for ids in (['synthetic-002'], ['unknown'], [], ['synthetic-001']*2):
        with pytest.raises(ValueError):
            live.select_development_cases(ids)


@pytest.mark.parametrize('argv', [['assessment','--case-id','synthetic-001'],
                                  ['assessment','--live','--case-id','unknown']])
def test_cli_guards_before_provider_construction(monkeypatch, argv):
    monkeypatch.setattr(sys,'argv',argv)
    monkeypatch.setattr(live,'AssessmentService', lambda: pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit):
        live.main()


def test_production_does_not_import_prototype():
    for path in Path('backend/app').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert 'assessment' not in (node.module or '')
            elif isinstance(node, ast.Import):
                assert all('assessment' not in alias.name for alias in node.names)

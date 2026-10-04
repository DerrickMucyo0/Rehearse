"""Offline structural diagnostics; no model intelligence or content persistence."""
import asyncio
import json
from dataclasses import asdict
from typing import get_args

import pytest

from app import reasoning as r
from evals.interviewer.assessment_live import parse_assessment
from evals.interviewer.assessment_independent import parse_independent_assessment
from evals.interviewer.assessment_only import parse_assessment_only
from evals.interviewer.evaluate import Case, evaluate, metrics

PARSERS = (r.parse_decision, parse_assessment, parse_independent_assessment, parse_assessment_only)


@pytest.mark.parametrize('parser', PARSERS)
@pytest.mark.parametrize('value,code', [(' \t\r\n','empty_content'), ('{} PRIVATE','trailing_data'),
    ('{"x":','json_error_at_end'), ('PRIVATE {}','other_json_syntax'),
    ('{"x":"\\q"}','other_json_syntax'), ('\ud800','other_parse_failure'),
    ('['*1100,'json_error_at_end')])
def test_categories(parser,value,code):
    with pytest.raises(r.InvalidDecision) as caught:parser(value)
    assert caught.value.invalid_reason=='json_syntax'
    assert caught.value.json_reason==code
    assert str(caught.value)==''


@pytest.mark.parametrize('parser',PARSERS)
@pytest.mark.parametrize('value,code',[('{"x":1,"x":2}','duplicate_json_key'),
    ('NaN','non_json_constant'), ('{}','schema_validation'), (None,'content_type'),
    (' '*8193,'content_size')])
def test_existing_categories(parser,value,code):
    with pytest.raises(r.InvalidDecision) as caught:parser(value)
    assert caught.value.invalid_reason==code
    assert caught.value.json_reason is None


def test_closed_enum_and_classifier_failure(monkeypatch):
    assert get_args(r.JsonReason)==('empty_content','trailing_data','json_error_at_end',
                                   'other_json_syntax','other_parse_failure')
    with pytest.raises(ValueError):r.InvalidDecision(invalid_reason='schema_validation',json_reason='empty_content')
    with pytest.raises(ValueError):r.InvalidDecision(invalid_reason='json_syntax',json_reason='PRIVATE')
    def broken(*args):raise RuntimeError('PRIVATE')
    monkeypatch.setattr(r,'_json_failure_category',broken)
    with pytest.raises(r.InvalidDecision) as caught:r.parse_decision('PRIVATE {}')
    assert caught.value.json_reason=='other_parse_failure' and str(caught.value)==''
    assert r.parse_decision('{"action":"MOVE_ON","reason":"Complete","next_prompt":null}').action=='MOVE_ON'


@pytest.mark.parametrize('parser',PARSERS)
def test_acceptance_and_values_against_original_path(parser):
    # Independent reference is the exact pre-patch parser, loaded without writing files.
    import types
    import importlib
    name={r.parse_decision:'reasoning',parse_assessment:'assessment_live',
          parse_independent_assessment:'assessment_independent',parse_assessment_only:'assessment_only'}[parser]
    reference=types.ModuleType('reference_'+name)
    module = importlib.import_module('app.reasoning' if name == 'reasoning' else 'evals.interviewer.' + name)
    reference.__dict__.update(vars(module))
    reference.__dict__.update(DuplicateJSONKey=r.DuplicateJSONKey, NonJSONConstant=r.NonJSONConstant, strict_json=r.strict_json)
    exec(compile(ORIGINAL_PARSERS[name], '<reference>', 'exec'), reference.__dict__)
    original=getattr(reference,parser.__name__)
    decision={'action':'MOVE_ON','reason':'Complete','next_prompt':None}
    assessment={'understandable_relevant':True,'essential_descriptive_gap':False,
                'unresolved_reasoning_issue':False,'reason':'Complete','next_prompt':None}
    only={k:v for k,v in assessment.items() if k!='next_prompt'}
    corpus=[json.dumps(x) for x in (decision,assessment,only,None,True,123,[],{})]
    corpus += ['', ' \t', 'PRIVATE {}', '{} PRIVATE', '{"x":', '{"x":"\\q"}',
               '{"x":1,"x":2}', 'NaN', '\ud800', '['*1100, ' '*8193, None]
    def result(fn,value):
        try:return ('valid',fn(value).model_dump())
        except Exception as exc:return ('invalid',getattr(exc,'invalid_reason',None),getattr(exc,'schema_reason',None))
    for value in corpus:assert result(parser,value)==result(original,value)


@pytest.mark.parametrize('error,invalid,code',[
    (None,None,None), (r.ReasoningTimeout(),None,None),
    (r.ReasoningFailed(failure=r.ProviderFailure(failure_kind='transport')),None,None),
    (r.InvalidDecision(invalid_reason='finish_reason'),'finish_reason',None),
    (r.InvalidDecision(invalid_reason='response_envelope'),'response_envelope',None),
    ('syntax','json_syntax','trailing_data')])
def test_evaluator_projection_and_scoring(error,invalid,code):
    case=Case(id='strong_complete-001',category='strong_complete',split='development',
        question='PRIVATE QUESTION',current_prompt='PRIVATE PROMPT',answer='PRIVATE ANSWER',
        expected_action='MOVE_ON',label_reason='Synthetic test',prior_turns=[])
    class Service:
        async def decide(self,context):
            if error=='syntax':return r.parse_decision('{} PRIVATE SECRET TRACE')
            if error is not None:raise error
            return r.Decision(action='MOVE_ON',reason='PRIVATE REASON',next_prompt=None)
    rows=asyncio.run(evaluate([case],Service()))
    row=asdict(rows[0]);assert row['invalid_reason']==invalid and row['json_reason']==code
    serialized=json.dumps(row)
    assert 'PRIVATE' not in serialized and 'SECRET' not in serialized and 'TRACE' not in serialized
    assert set(row)=={'case_id','category','expected','predicted','error','latency_ms',
                      'repeat','failure_kind','http_status','invalid_reason','json_reason'}
    before=metrics(rows)
    from dataclasses import replace
    assert metrics([replace(rows[0],json_reason=None)])==before

# Frozen pre-patch acceptance paths; validators remain covered by existing contract tests.
ORIGINAL_PARSERS = {
    'reasoning': """
def parse_decision(value: str) -> Decision:
    if type(value) is not str:
        raise InvalidDecision(invalid_reason='content_type') from None
    try:
        if len(value.encode('utf-8')) > 8192:
            raise InvalidDecision(invalid_reason='content_size') from None
        decoded = strict_json(value)
    except DuplicateJSONKey:
        raise InvalidDecision(invalid_reason='duplicate_json_key') from None
    except NonJSONConstant:
        raise InvalidDecision(invalid_reason='non_json_constant') from None
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='json_syntax') from None
    try:
        return Decision.model_validate(decoded)
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='schema_validation') from None
""",
    'assessment_live': """
def parse_assessment(value: str) -> Assessment:
    if type(value) is not str:
        raise InvalidDecision(invalid_reason='content_type') from None
    try:
        if len(value.encode('utf-8')) > 8192:
            raise InvalidDecision(invalid_reason='content_size') from None
        decoded = strict_json(value)
    except DuplicateJSONKey:
        raise InvalidDecision(invalid_reason='duplicate_json_key') from None
    except NonJSONConstant:
        raise InvalidDecision(invalid_reason='non_json_constant') from None
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='json_syntax') from None
    try:
        return Assessment.model_validate(decoded)
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='schema_validation') from None
""",
    'assessment_independent': """
def parse_independent_assessment(value: str) -> IndependentAssessment:
    if type(value) is not str:
        raise InvalidDecision(invalid_reason='content_type') from None
    try:
        if len(value.encode('utf-8')) > 8192:
            raise InvalidDecision(invalid_reason='content_size') from None
        decoded = strict_json(value)
    except DuplicateJSONKey:
        raise InvalidDecision(invalid_reason='duplicate_json_key') from None
    except NonJSONConstant:
        raise InvalidDecision(invalid_reason='non_json_constant') from None
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='json_syntax') from None
    return validate_assessment(decoded)
""",
    'assessment_only': """
def parse_assessment_only(value: str) -> AssessmentOnly:
    if type(value) is not str:
        raise InvalidDecision(invalid_reason='content_type') from None
    try:
        if len(value.encode('utf-8')) > 8192:
            raise InvalidDecision(invalid_reason='content_size') from None
        decoded = strict_json(value)
    except DuplicateJSONKey:
        raise InvalidDecision(invalid_reason='duplicate_json_key') from None
    except NonJSONConstant:
        raise InvalidDecision(invalid_reason='non_json_constant') from None
    except (ValueError, TypeError, RecursionError):
        raise InvalidDecision(invalid_reason='json_syntax') from None
    return validate_assessment(decoded)
""",
}

@pytest.mark.parametrize('failure',[RecursionError,TypeError])
def test_other_existing_parse_exceptions(monkeypatch,failure):
    def rejected(value):raise failure('PRIVATE EXCEPTION')
    monkeypatch.setattr(r,'strict_json',rejected)
    with pytest.raises(r.InvalidDecision) as caught:r.parse_decision('{}')
    assert caught.value.invalid_reason=='json_syntax'
    assert caught.value.json_reason=='other_parse_failure'
    assert str(caught.value)==''


@pytest.mark.parametrize('invalid',get_args(r.InvalidReason))
def test_non_json_categories_have_no_json_reason(invalid):
    error=r.InvalidDecision(invalid_reason=invalid)
    assert error.json_reason is None
    if invalid!='json_syntax':
        with pytest.raises(ValueError):r.InvalidDecision(invalid_reason=invalid,json_reason='other_parse_failure')


def test_diagnostic_failure_does_not_escape_on_prefix_probe(monkeypatch):
    def broken(*args,**kwargs):raise RuntimeError('PRIVATE DIAGNOSTIC')
    monkeypatch.setattr(r.json.JSONDecoder,'raw_decode',broken)
    # Force the original strict parser failure independently of diagnostic decoding.
    def original(value):raise json.JSONDecodeError('PRIVATE PARSER',value,0)
    monkeypatch.setattr(r,'strict_json',original)
    with pytest.raises(r.InvalidDecision) as caught:r.parse_decision('{} PRIVATE')
    assert caught.value.invalid_reason=='json_syntax'
    assert caught.value.json_reason=='other_parse_failure'
    assert str(caught.value)==''

@pytest.mark.parametrize('module_name', ['assessment_live','assessment_live_v2',
                                       'assessment_live_v3','assessment_only_live'])
def test_assessment_reports_propagate_only_safe_metadata(module_name,monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "OFFLINE_TEST_ONLY")
    import importlib
    import httpx
    module=importlib.import_module('evals.interviewer.'+module_name)
    case=Case(id='unsupported_claim-003',category='unsupported_claim',split='development',
        question='PRIVATE QUESTION',current_prompt='PRIVATE PROMPT',answer='PRIVATE ANSWER',
        expected_action='CHALLENGE',label_reason='Synthetic test',prior_turns=[])
    def handler(request):
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':'{} PRIVATE RAW CONTENT',
            'reasoning_content':'PRIVATE HIDDEN TRACE'}}]})
    report=asyncio.run(module.assessment_report([case],module.AssessmentService(httpx.MockTransport(handler))))
    row=report['outcomes'][0]
    assert row['error']=='invalid_output' and row['invalid_reason']=='json_syntax'
    assert row['json_reason']=='trailing_data'
    assert row.get('schema_reason') is None
    assert 'PRIVATE' not in json.dumps(report)

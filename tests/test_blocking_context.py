"""Offline contract, reviewed-selection, transport and privacy checks."""
import asyncio
import ast
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.reasoning import InvalidDecision
from evals.interviewer import blocking_context_live as live
from evals.interviewer.blocking_context import (
    BlockingContextAssessment, InvalidAssessment, parse_assessment,
    validate_assessment, map_assessment)

FIELDS = live.SEMANTIC_FIELDS
KEYS = list(itertools.product((True, False, None), repeat=3))
VALID = [(False, None, None), *[(True, g, r) for g, r in itertools.product((False, True), repeat=2)]]


def action(key):
    u, gap, issue = key
    return 'CLARIFY' if not u else 'FOLLOW_UP' if gap else 'CHALLENGE' if issue else 'MOVE_ON'


def payload(key=(True, False, True), **updates):
    result = dict(zip(FIELDS, key), reason='PRIVATE_REASON',
                  next_prompt=None if action(key) == 'MOVE_ON' else 'PRIVATE_PROMPT')
    result.update(updates)
    return result


@pytest.mark.parametrize('key', KEYS)
def test_exact_semantic_combinations_and_mapping(key):
    if key in VALID:
        parsed = parse_assessment(json.dumps(payload(key)))
        assert map_assessment(parsed) == action(key)
        assert tuple(getattr(parsed, f) for f in FIELDS) == key
    else:
        with pytest.raises(InvalidAssessment) as caught:
            parse_assessment(json.dumps(payload(key)))
        assert caught.value.invalid_reason == 'schema_validation'
        assert caught.value.schema_reason in ('strict_type_violation', 'invalid_assessment_combination')
        assert caught.value.json_reason is None


def test_schema_and_instance_revalidation():
    assert set(BlockingContextAssessment.model_fields) == {*FIELDS, 'reason', 'next_prompt'}
    schema = BlockingContextAssessment.model_json_schema()
    assert set(schema['required']) == {*FIELDS, 'reason', 'next_prompt'}
    assert schema['additionalProperties'] is False
    assert 'essential_descriptive_gap' not in json.dumps(schema)
    forged = BlockingContextAssessment.model_construct(**payload((True, None, True)))
    with pytest.raises(InvalidAssessment): map_assessment(forged)
    both = validate_assessment(payload((True, True, True)))
    assert map_assessment(both) == 'FOLLOW_UP' and both.unresolved_reasoning_issue is True
    with pytest.raises(ValueError): both.blocking_context_gap = False


@pytest.mark.parametrize('update,code', [
    ({'understandable_relevant': 1}, 'strict_type_violation'),
    ({'blocking_context_gap': 'false'}, 'strict_type_violation'),
    ({'unresolved_reasoning_issue': 'true'}, 'strict_type_violation'),
    ({'reason': 4}, 'strict_type_violation'),
    ({'reason': ''}, 'text_bound_violation'),
    ({'reason': 'x'*301}, 'text_bound_violation'),
    ({'next_prompt': ' '}, 'text_bound_violation'),
    ({'next_prompt': 'x'*501}, 'text_bound_violation'),
    ({'next_prompt': None}, 'assessment_prompt_inconsistency'),
    ({'essential_descriptive_gap': False}, 'forbidden_extra_field'),
    ({'action': 'CHALLENGE'}, 'forbidden_extra_field')])
def test_schema_diagnostics(update,code):
    with pytest.raises(InvalidAssessment) as caught: validate_assessment(payload(**update))
    assert caught.value.schema_reason == code
    assert str(caught.value) == ''


@pytest.mark.parametrize('field', [*FIELDS, 'reason', 'next_prompt'])
def test_all_fields_required(field):
    data=payload();data.pop(field)
    with pytest.raises(InvalidAssessment) as caught:validate_assessment(data)
    assert caught.value.schema_reason == 'missing_required_field'


def test_prompt_consistency_and_bounds():
    with pytest.raises(InvalidAssessment) as caught:
        validate_assessment(payload((True, False, False), next_prompt='Probe'))
    assert caught.value.schema_reason == 'assessment_prompt_inconsistency'
    parsed = validate_assessment(payload(reason=' x ',next_prompt=' question '))
    assert parsed.reason == 'x' and parsed.next_prompt == 'question'


@pytest.mark.parametrize('content,invalid,json_reason', [
    (' \t\n', 'json_syntax', 'empty_content'),
    ('{} PRIVATE', 'json_syntax', 'trailing_data'),
    ('{"x":', 'json_syntax', 'json_error_at_end'),
    ('PRIVATE {}', 'json_syntax', 'other_json_syntax'),
    ('\ud800', 'json_syntax', 'other_parse_failure'),
    ('{"x":1,"x":2}', 'duplicate_json_key', None),
    ('NaN', 'non_json_constant', None),
    (None, 'content_type', None),
    (' '*8193, 'content_size', None)])
def test_strict_parser_diagnostics_no_repair(content,invalid,json_reason):
    with pytest.raises(InvalidDecision) as caught:parse_assessment(content)
    assert (caught.value.invalid_reason,caught.value.json_reason)==(invalid,json_reason)
    assert str(caught.value)==''


def test_reviewed_eleven_cases_and_mapping():
    ids=[*live.ORIGINAL_IDS,*live.BOUNDARY_IDS]
    cases=live.select_development_cases(ids)
    expected=live.reviewed_expectations(cases)
    assert [c.id for c in cases] == ids
    assert all(c.split=='development' for c in cases)
    for case,row in zip(cases,expected):
        key=tuple(getattr(row,f) for f in FIELDS)
        assert map_assessment(validate_assessment(payload(key))) == case.expected_action
    assert all(tuple(getattr(row,f) for f in FIELDS)==(True,True,True) for row in expected[-3:])
    boundaries=json.loads(live.BOUNDARIES.read_text())
    assert [r['review_example'] for r in boundaries['examples']]==['B1','B2','B3']
    assert all(r['review_outcome']=='APPROVE' for r in boundaries['examples'])


@pytest.mark.parametrize('mutation',['held_out','answer','expected_action','unknown','duplicate','empty'])
def test_guard_before_service_calls(mutation):
    cases=live.select_development_cases(['unsupported_claim-003'])
    if mutation=='held_out':cases=[cases[0].model_copy(update={'split':'held_out'})]
    elif mutation=='answer':cases=[cases[0].model_copy(update={'answer':'Changed source'})]
    elif mutation=='expected_action':cases=[cases[0].model_copy(update={'expected_action':'MOVE_ON'})]
    elif mutation=='unknown':cases=[cases[0].model_copy(update={'id':'unknown-001'})]
    elif mutation=='duplicate':cases=cases*2
    else:cases=[]
    class Forbidden:
        async def decide(self,context):pytest.fail('Provider called')
    with pytest.raises(ValueError):asyncio.run(live.assessment_report(cases,Forbidden()))


@pytest.mark.parametrize('ids',[[],['unknown-001'],['unsupported_claim-003']*2])
def test_selection_rejection(ids):
    with pytest.raises(ValueError):live.select_development_cases(ids)


def test_review_artifact_tampering_rejected(tmp_path,monkeypatch):
    artifact=json.loads(live.REVIEWED_ANNOTATIONS.read_text())
    artifact['annotations'][0]['review_outcome']='PENDING'
    path=tmp_path/'review.json';path.write_text(json.dumps(artifact))
    monkeypatch.setattr(live,'REVIEWED_ANNOTATIONS',path)
    with pytest.raises(ValueError):live.reviewed_expectations(live.select_development_cases(['unsupported_claim-003']))


@pytest.mark.parametrize('argv', [
    ['blocking_context','--case-id','unsupported_claim-003'],
    ['blocking_context','--live','--case-id','unknown-001'],
    ['blocking_context','--live'],
    ['blocking_context','--live','--case-id','unsupported_claim-003','--split','held_out']])
def test_cli_guards_before_provider_construction(monkeypatch,argv):
    monkeypatch.setattr(sys,'argv',argv)
    monkeypatch.setattr(live,'AssessmentService',lambda:pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit):live.main()


@pytest.mark.parametrize('mode,error,invalid,json_reason,schema,kind,status',[
    ('valid',None,None,None,None,None,None),
    ('syntax','invalid_output','json_syntax','trailing_data',None,None,None),
    ('schema','invalid_output','schema_validation',None,'missing_required_field',None,None),
    ('envelope','invalid_output','response_envelope',None,None,None,None),
    ('finish','invalid_output','finish_reason',None,None,None,None),
    ('refusal','invalid_output','refusal',None,None,None,None),
    ('tool','invalid_output','tool_or_function_call',None,None,None,None),
    ('http','provider_error',None,None,None,'http_status',429),
    ('transport','provider_error',None,None,None,'transport',None),
    ('timeout','timeout',None,None,None,None,None),
    ('unavailable','provider_error',None,None,None,'unavailable',None)])
def test_mock_provider_report_privacy_and_diagnostics(monkeypatch,mode,error,invalid,json_reason,schema,kind,status):
    monkeypatch.setenv('NVIDIA_API_KEY','PRIVATE_KEY')
    calls=[]
    def handler(request):
        calls.append(request)
        sent=json.loads(request.content)
        assert sent['model']==live.MODEL and sent['stream'] is False
        assert {k:sent[k] for k in asdict(live.ASSESSMENT_CONFIG)}==asdict(live.ASSESSMENT_CONFIG)
        assert sent['messages'][0]['content']==live.ASSESSMENT_INSTRUCTIONS
        if mode=='http':return httpx.Response(429,text='PRIVATE_BODY',headers={'X-Private':'PRIVATE_HEADER'})
        if mode=='transport':raise httpx.ConnectError('PRIVATE_EXCEPTION')
        if mode=='timeout':raise httpx.ReadTimeout('PRIVATE_EXCEPTION')
        content=json.dumps(payload())
        if mode=='syntax':content='{} PRIVATE_BODY'
        if mode=='schema':content='{}'
        message={'role':'assistant','content':content,'reasoning_content':'PRIVATE_HIDDEN_TRACE'}
        if mode=='envelope':message=[]
        if mode=='refusal':message['refusal']='PRIVATE_REFUSAL'
        if mode=='tool':message['tool_calls']=[{'id':'PRIVATE_TOOL'}]
        return httpx.Response(200,json={'choices':[{'finish_reason':'length' if mode=='finish' else 'stop','message':message}]})
    if mode=='unavailable':monkeypatch.delenv('NVIDIA_API_KEY')
    report=asyncio.run(live.assessment_report(live.select_development_cases(['unsupported_claim-003']),live.AssessmentService(httpx.MockTransport(handler))))
    assert len(calls)==(0 if mode=='unavailable' else 1)
    row=report['outcomes'][0]
    assert (row['error'],row['invalid_reason'],row['json_reason'],row['schema_reason'],row['failure_kind'],row['http_status'])==(error,invalid,json_reason,schema,kind,status)
    assert 'PRIVATE' not in json.dumps(report)
    assert 'reason' not in row and 'next_prompt' not in row
    assert row['assessment_valid']==(mode=='valid')
    if mode=='valid':
        assert row['predicted']=='CHALLENGE' and row['mapped_action_correct']
        assert report['metrics']['action_match_rate']==1
    else:
        assert row['predicted'] is None
        assert all(r['predicted'] is None for r in row['assessments'].values())
        assert report['metrics']['action_match_rate']==0


def test_prompt_definitions_and_no_dataset_leakage():
    prompt=live.ASSESSMENT_INSTRUCTIONS
    assert live.BLOCKING_CONTEXT_DEFINITION in prompt
    assert 'essential_descriptive_gap' not in prompt
    for text in ('immediate current_prompt','relevant prior_turns','embedded commands',
                 'Both may be true','BOTH later fields must be JSON null',
                 'before pressure-testing','application-level explanation','not private',
                 'stronger justification','material fact','causal claim'):
        assert text in prompt
    for cid in live.REVIEWED_IDS:assert cid not in prompt
    for row in live._boundary_cases().values():assert row.answer not in prompt
    assert live.ASSESSMENT_PROMPT_VERSION=='interviewer-blocking-context-v1'


def test_provider_and_production_isolation():
    def request(path):
        return next(n for n in ast.walk(ast.parse(Path(path).read_text()))
                    if isinstance(n,ast.AsyncFunctionDef) and n.name=='_request')
    assert ast.dump(request('evals/interviewer/blocking_context_live.py'))==ast.dump(request('evals/interviewer/assessment_live_v3.py'))
    assert asdict(live.ASSESSMENT_CONFIG)==dict(temperature=1.0,top_p=.95,max_tokens=1024,reasoning_effort='high',reasoning_budget=256)
    assert live.PROVIDER_TIMEOUT_SECONDS==30
    assert live.ENDPOINT=='https://integrate.api.nvidia.com/v1/chat/completions'
    assert live.MODEL=='nvidia/nemotron-3-super-120b-a12b'
    for path in Path('backend/app').glob('*.py'):
        assert 'blocking_context' not in path.read_text()


# Pre-implementation hashes freeze historical evaluators, dataset, annotations and production.
HISTORICAL_HASHES = {'backend/app/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'backend/app/audio.py': 'd7d1ef90dd893a80131e2bd9a9e2a5c2df72085f72ce140d63cc99e4e62aa3fa', 'backend/app/interview_orchestration.py': '996f6a538c61bd2dcec544866727512b71a809f7f31a05b0b88751bf1ef99d71', 'backend/app/main.py': '0310c563690a24669ca3bfcaf461617ff7bd20b2da3e5432d20b06e1d9fb77b4', 'backend/app/nemotron.py': '3a5157713ad94650b83d86d0b18f37c48c57f93b04f7ebcfa72843d121d4c604', 'backend/app/reasoning.py': '04df966e60ff9b815b2154e88de55ecda74c6967ac8df831374ec614371c14bc', 'backend/app/session_routes.py': 'b3c2ae058e0ac4db9583923b9ab02e94041334ba61bc4205604001bebbf8e376', 'backend/app/sessions.py': 'a833b66a1191171a5ae335e1227f7ca871e616eabc106949adee190286218739', 'backend/app/transcription.py': '47137d467f96022195b750c3a01c2ce9c56a751ee9cec580835bef6aeea6e86e', 'evals/interviewer/ASSESSMENT.md': '2b551bc8f49463e47509730bcaca64c38e383f970b610ff225db7ed3d819120c', 'evals/interviewer/ASSESSMENT_ONLY.md': 'ac355db6fda39eee8f4ba644bb54d86e060f66c802daf07924bb026c79c7275f', 'evals/interviewer/ASSESSMENT_V2.md': '3c2accddb5fc01a3d65b20bbd3639fc07ed405e925c93657a85123bb4045e077', 'evals/interviewer/README.md': '5144fa63602e53f7f58fed5db88b797b9fb975d54c1e4ba6a91998041181c591', 'evals/interviewer/assessment_independent.py': 'e0bed7d6e1d85ae462e6ce2fd622897481236366bc56700a7ac17ce1a3e96f4f', 'evals/interviewer/assessment_live.py': '8673c8548cfe10a9b167ce7b8d2dd4327dbb1c9686bdbf7e88e3bdfe772bcf6e', 'evals/interviewer/assessment_live_v2.py': '5075fb91e646e0179a761560200eaa5a698c532da29f159d670d2fe52166123c', 'evals/interviewer/assessment_live_v3.py': '4af0dddf5b3f18863d188df2cccce3e9ef93e71cdaa2626bd5b48e845777daf2', 'evals/interviewer/assessment_only.py': '2a938d23f44ad180fc8c5c09d47aadd99822916de1f17848510ed95668f9ef90', 'evals/interviewer/assessment_only_live.py': 'a7b4717717784e1bd2e0889ff70105968e3c713741c40a8758bf1e48aba6538f', 'evals/interviewer/assessment_prototype.py': '1c8aadb9915ff968086b8c107cd909b6d2a367cec64db2399ad8ca51e729cf9e', 'evals/interviewer/cases.jsonl': 'd496391a3f25ac8173e504794497c4aa4120526a0c7238f94164b3bd234dfc35', 'evals/interviewer/evaluate.py': '3c6f4db841cd8df3e72b5211f430bb807a208c22221efbc14c0fba9557975309', 'evals/interviewer/independent_annotations_development_draft.json': '24723e8e24ab6d025709b9e59a09e122c7fc390af20d8ff250487960f8ed350f'}

def test_historical_and_production_files_unchanged():
    for path, digest in HISTORICAL_HASHES.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path

"""Offline conditional-gate contracts, deadline, routing, sessions and privacy."""
import asyncio
import ast
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from app.reasoning import (Decision, InvalidDecision, ReasoningContext, ReasoningFailed,
                           ReasoningTimeout, ReasoningUnavailable, ProviderFailure)
from app.sessions import InterviewSessionService, AnswerRequest, SessionConflict
from evals.interviewer import two_stage_contract as contract
from evals.interviewer import two_stage_orchestration as orchestration
from evals.interviewer import two_stage_provider as provider
from evals.interviewer import two_stage_live as live
from evals.interviewer import blocking_context_live as reviewed


def first(u=True,gap=False,prompt=None,**updates):
    value=dict(understandable_relevant=u,blocking_context_gap=gap,
               reason='PRIVATE_STAGE1_REASON',next_prompt=prompt)
    value.update(updates)
    return value


def second(issue=True,prompt='PRIVATE_STAGE2_PROMPT',**updates):
    value=dict(unresolved_reasoning_issue=issue,reason='PRIVATE_STAGE2_REASON',next_prompt=prompt)
    value.update(updates)
    return value


def context():
    return ReasoningContext(question='PRIVATE_QUESTION',current_prompt='PRIVATE_CURRENT',
                            answer='PRIVATE_ANSWER')


class Fake:
    def __init__(self,value=None,error=None):
        self.value,self.error,self.contexts=value,error,[]
    async def decide(self,ctx):
        self.contexts.append(ctx)
        if self.error:raise self.error
        return self.value


@pytest.mark.parametrize('u,gap,prompt',list(itertools.product((True,False,None),(True,False,None),(None,'Prompt'))))
def test_stage1_complete_matrix(u,gap,prompt):
    valid=(u is False and gap is None and prompt is not None) or (
        u is True and type(gap) is bool and ((gap is False)==(prompt is None)))
    if valid:
        result=contract.parse_stage1(json.dumps(first(u,gap,prompt)))
        assert contract.route_stage1(result)==('CLARIFY' if not u else 'FOLLOW_UP' if gap else 'CONTINUE_TO_STAGE_2')
    else:
        with pytest.raises(InvalidDecision):contract.parse_stage1(json.dumps(first(u,gap,prompt)))


@pytest.mark.parametrize('issue,prompt',list(itertools.product((True,False,None,1,'true'),(None,'Prompt'))))
def test_stage2_complete_matrix(issue,prompt):
    if type(issue) is bool and issue==(prompt is not None):
        result=contract.parse_stage2(json.dumps(second(issue,prompt)))
        assert contract.map_stage2(result).action==('CHALLENGE' if issue else 'MOVE_ON')
    else:
        with pytest.raises(InvalidDecision):contract.parse_stage2(json.dumps(second(issue,prompt)))


@pytest.mark.parametrize('model,parser,payload,fields',[
    (contract.Stage1,contract.parse_stage1,first,{'understandable_relevant','blocking_context_gap','reason','next_prompt'}),
    (contract.Stage2,contract.parse_stage2,second,{'unresolved_reasoning_issue','reason','next_prompt'})])
def test_exact_schema_missing_fields(model,parser,payload,fields):
    schema=model.model_json_schema()
    assert set(schema['properties'])==fields and set(schema['required'])==fields
    assert schema['additionalProperties'] is False
    for name in fields:
        value=payload();value.pop(name)
        with pytest.raises(contract.InvalidAssessment) as caught:parser(json.dumps(value))
        assert caught.value.schema_reason=='missing_required_field'


@pytest.mark.parametrize('parser,payload,extras',[
    (contract.parse_stage1,first,['unresolved_reasoning_issue','action','issue_priority']),
    (contract.parse_stage2,second,['understandable_relevant','blocking_context_gap','action'])])
def test_forbidden_extras(parser,payload,extras):
    for field in extras:
        value=payload();value[field]=True
        with pytest.raises(contract.InvalidAssessment) as caught:parser(json.dumps(value))
        assert caught.value.schema_reason=='forbidden_extra_field'


@pytest.mark.parametrize('parser,payload',[(contract.parse_stage1,first),(contract.parse_stage2,second)])
@pytest.mark.parametrize('field,value,code',[
    ('reason',1,'strict_type_violation'),('reason',' ','text_bound_violation'),
    ('reason','x'*301,'text_bound_violation'),('next_prompt',1,'strict_type_violation'),
    ('next_prompt',' ','text_bound_violation'),('next_prompt','x'*501,'text_bound_violation')])
def test_text_constraints(parser,payload,field,value,code):
    data=payload();data[field]=value
    with pytest.raises(contract.InvalidAssessment) as caught:parser(json.dumps(data))
    assert caught.value.schema_reason==code and str(caught.value)==''


@pytest.mark.parametrize('payload,validator,field',[
    (first,contract.validate_stage1,'understandable_relevant'),
    (first,contract.validate_stage1,'blocking_context_gap'),
    (second,contract.validate_stage2,'unresolved_reasoning_issue')])
@pytest.mark.parametrize('value',[0,1,'false','true',[],{}])
def test_strict_booleans(payload,validator,field,value):
    data=payload();data[field]=value
    with pytest.raises(contract.InvalidAssessment) as caught:validator(data)
    assert caught.value.schema_reason=='strict_type_violation'


@pytest.mark.parametrize('parser',[contract.parse_stage1,contract.parse_stage2])
@pytest.mark.parametrize('value,invalid,category',[
    (' ', 'json_syntax','empty_content'),('{} PRIVATE','json_syntax','trailing_data'),
    ('{"x":','json_syntax','json_error_at_end'),('PRIVATE {}','json_syntax','other_json_syntax'),
    ('\ud800','json_syntax','other_parse_failure'),('{"x":1,"x":2}','duplicate_json_key',None),
    ('NaN','non_json_constant',None),(None,'content_type',None),(' '*8193,'content_size',None)])
def test_shared_strict_parser_no_repair(parser,value,invalid,category):
    with pytest.raises(InvalidDecision) as caught:parser(value)
    assert (caught.value.invalid_reason,caught.value.json_reason)==(invalid,category)
    assert str(caught.value)==''


def test_frozen_and_revalidated_instances():
    valid=contract.validate_stage1(first())
    with pytest.raises(ValueError):valid.reason='changed'
    forged=contract.Stage1.model_construct(**first(True,None))
    with pytest.raises(InvalidDecision):contract.route_stage1(forged)
    forged=contract.Stage2.model_construct(**second(False,'Prompt'))
    with pytest.raises(InvalidDecision):contract.map_stage2(forged)
    with pytest.raises(ValueError):contract.terminal_stage1(valid)


@pytest.mark.parametrize('value,action',[ (first(False,None,'Meaning?'),'CLARIFY'),
                                       (first(True,True,'Result?'),'FOLLOW_UP')])
def test_terminal_skips_stage2(value,action):
    one,two=Fake(value),Fake(error=AssertionError('No Stage 2'))
    reasoner=orchestration.TwoStageReasoner(one,two)
    assert asyncio.run(reasoner.decide(context())).action==action
    assert len(one.contexts)==1 and not two.contexts
    assert reasoner.last_trace.route==action


@pytest.mark.parametrize('issue,action',[(True,'CHALLENGE'),(False,'MOVE_ON')])
def test_continue_exactly_one_stage2_and_context_only(issue,action):
    one,two=Fake(first()),Fake(second(issue,None if not issue else 'Probe'))
    reasoner=orchestration.TwoStageReasoner(one,two)
    assert asyncio.run(reasoner.decide(context())).action==action
    assert len(one.contexts)==len(two.contexts)==1
    assert one.contexts[0] is two.contexts[0]
    assert 'PRIVATE_STAGE1_REASON' not in two.contexts[0].model_dump_json()


@pytest.mark.parametrize('stage',[1,2])
@pytest.mark.parametrize('failure',[InvalidDecision(invalid_reason='json_syntax',json_reason='other_json_syntax'),
    ReasoningTimeout(),ReasoningUnavailable(),ReasoningFailed(failure=ProviderFailure(failure_kind='transport'))])
def test_failures_no_final_action_or_retry(stage,failure):
    one=Fake(first(),failure if stage==1 else None)
    two=Fake(second(),failure if stage==2 else None)
    reasoner=orchestration.TwoStageReasoner(one,two)
    with pytest.raises(type(failure)):asyncio.run(reasoner.decide(context()))
    assert len(one.contexts)==1 and len(two.contexts)==(stage==2)
    assert reasoner.last_trace.failure_stage==f'stage_{stage}'
    assert reasoner.last_trace.stage_2.unresolved_reasoning_issue is None


class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value


def test_remaining_budget_computed_not_restarted(monkeypatch):
    clock=Clock();budgets=[]
    async def wait(awaitable,timeout):
        budgets.append(timeout)
        result=await awaitable
        clock.value+=7 if len(budgets)==1 else 4
        return result
    monkeypatch.setattr(orchestration,'wait_for',wait)
    reasoner=orchestration.TwoStageReasoner(Fake(first()),Fake(second()),clock=clock)
    assert asyncio.run(reasoner.decide(context())).action=='CHALLENGE'
    assert budgets==[30,23]
    assert reasoner.last_trace.latency_ms==11000


def test_exhausted_before_stage2_skips_call(monkeypatch):
    clock=Clock();original=orchestration.route_stage1
    def route(value):
        answer=original(value);clock.value=31;return answer
    monkeypatch.setattr(orchestration,'route_stage1',route)
    one,two=Fake(first()),Fake(second())
    reasoner=orchestration.TwoStageReasoner(one,two,clock=clock)
    with pytest.raises(ReasoningTimeout):asyncio.run(reasoner.decide(context()))
    assert not two.contexts and not reasoner.last_trace.stage_2.attempted
    assert reasoner.last_trace.failure_stage=='stage_2'


def test_real_shared_timeout_cancels_stage2(monkeypatch):
    monkeypatch.setattr(orchestration,'COMBINED_DEADLINE_SECONDS',.025)
    class Waiting(Fake):
        cancelled=False
        async def decide(self,ctx):
            self.contexts.append(ctx)
            try:await asyncio.Event().wait()
            finally:self.cancelled=True
    two=Waiting()
    reasoner=orchestration.TwoStageReasoner(Fake(first()),two)
    with pytest.raises(ReasoningTimeout):asyncio.run(reasoner.decide(context()))
    assert two.cancelled and len(two.contexts)==1
    assert reasoner.last_trace.failure_stage=='stage_2'
    assert reasoner.last_trace.stage_2.error=='timeout'


def request(session,text='Draft',submission_id=None):
    return AnswerRequest(question_index=session.current_question_index,turn_revision=session.turn_revision,
                         submission_id=submission_id or uuid4(),answer=text)


@pytest.mark.parametrize('stage',[1,2])
@pytest.mark.parametrize('failure',[ReasoningTimeout(),InvalidDecision(invalid_reason='schema_validation'),
                                    ReasoningFailed(failure=ProviderFailure(failure_kind='http_status',http_status=429))])
def test_session_failure_atomicity(stage,failure):
    sessions=InterviewSessionService();session=sessions.start();answer=request(session)
    one,two=Fake(first(),failure if stage==1 else None),Fake(second(),failure if stage==2 else None)
    reasoner=orchestration.TwoStageReasoner(one,two)
    with pytest.raises(type(failure)):
        asyncio.run(orchestration.submit_experimental(sessions,session.id,answer,reasoner))
    assert sessions.get(session.id)==session and session.id not in sessions._pending
    # Draft remains owned by caller and can be submitted with the same identifier.
    assert answer.answer=='Draft'
    replacement=orchestration.TwoStageReasoner(Fake(first()),Fake(second(False,None)))
    updated=asyncio.run(orchestration.submit_experimental(sessions,session.id,answer,replacement))
    assert updated.turn_revision==1 and updated.answers==['Draft']


@pytest.mark.parametrize('stage',[1,2])
def test_stale_after_either_call(stage):
    sessions=InterviewSessionService();session=sessions.start()
    class Stale(Fake):
        async def decide(self,ctx):
            value=await super().decide(ctx)
            with sessions._lock:sessions._sessions[session.id].turn_revision+=1
            return value
    one=Stale(first()) if stage==1 else Fake(first())
    two=Stale(second()) if stage==2 else Fake(second())
    reasoner=orchestration.TwoStageReasoner(one,two)
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions,session.id,request(session),reasoner))
    current=sessions.get(session.id)
    assert current.turn_revision==1 and current.answers==[] and current.turns==[] and current.probe_count==0
    assert len(two.contexts)==(stage==2)
    assert session.id not in sessions._pending
    assert reasoner.last_trace.failure_stage==f'stage_{stage}'


def test_idempotent_replay_and_probe_limit():
    sessions=InterviewSessionService();session=sessions.start();answer=request(session)
    one,two=Fake(first()),Fake(second(False,None))
    reasoner=orchestration.TwoStageReasoner(one,two)
    updated=asyncio.run(orchestration.submit_experimental(sessions,session.id,answer,reasoner))
    assert asyncio.run(orchestration.submit_experimental(sessions,session.id,answer,reasoner))==updated
    assert len(one.contexts)==len(two.contexts)==1
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions,session.id,answer.model_copy(update={'answer':'Changed'}),reasoner))
    probes=Fake(first(True,True,'Result?'));never=Fake(error=AssertionError('Stage 2 forbidden'))
    reasoner=orchestration.TwoStageReasoner(probes,never)
    for _ in range(3):
        updated=asyncio.run(orchestration.submit_experimental(sessions,session.id,request(updated),reasoner))
    assert len(probes.contexts)==2 and not never.contexts
    assert updated.turns[-1].transition_source=='probe_limit' and updated.turns[-1].action is None
    assert updated.turn_revision==4 and updated.current_question_index==2


@pytest.mark.parametrize('stage',[1,2])
def test_no_lock_across_wait_and_cancellation(stage):
    async def scenario():
        sessions=InterviewSessionService();session=sessions.start()
        entered=asyncio.Event()
        class Waiting(Fake):
            async def decide(self,ctx):
                self.contexts.append(ctx);entered.set();await asyncio.Event().wait()
        one=Waiting() if stage==1 else Fake(first())
        two=Waiting() if stage==2 else Fake(second())
        reasoner=orchestration.TwoStageReasoner(one,two)
        answer=request(session)
        task=asyncio.create_task(orchestration.submit_experimental(sessions,session.id,answer,reasoner))
        await entered.wait()
        assert sessions._lock.acquire(blocking=False);sessions._lock.release()
        assert sessions.get(session.id)==session
        assert sessions.start().id!=session.id
        with pytest.raises(SessionConflict):await orchestration.submit_experimental(sessions,session.id,answer,reasoner)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert sessions.get(session.id)==session and session.id not in sessions._pending
    asyncio.run(scenario())


@pytest.mark.parametrize('stage',[1,2])
@pytest.mark.parametrize('mode,error,invalid,json_reason,schema,kind,status',[
    ('syntax','invalid_output','json_syntax','trailing_data',None,None,None),
    ('schema','invalid_output','schema_validation',None,'missing_required_field',None,None),
    ('envelope','invalid_output','response_envelope',None,None,None,None),
    ('finish','invalid_output','finish_reason',None,None,None,None),
    ('refusal','invalid_output','refusal',None,None,None,None),
    ('tool','invalid_output','tool_or_function_call',None,None,None,None),
    ('size','invalid_output','response_size',None,None,None,None),
    ('http','provider_error',None,None,None,'http_status',429),
    ('transport','provider_error',None,None,None,'transport',None),
    ('timeout','timeout',None,None,None,None,None),
    ('adapter','provider_error',None,None,None,'adapter_error',None)])
def test_mock_transport_stage_failure_report(monkeypatch,stage,mode,error,invalid,json_reason,schema,kind,status):
    monkeypatch.setenv('NVIDIA_API_KEY','PRIVATE_KEY')
    calls=[]
    def handler(req):
        calls.append(json.loads(req.content))
        is_stage2='Stage 2.' in calls[-1]['messages'][0]['content']
        if len(calls)!=stage:
            content=json.dumps(first())
        else:
            if mode=='http':return httpx.Response(429,text='PRIVATE_BODY')
            if mode=='transport':raise httpx.ConnectError('PRIVATE_EXCEPTION')
            if mode=='timeout':raise httpx.ReadTimeout('PRIVATE_EXCEPTION')
            if mode=='adapter':raise RuntimeError('PRIVATE_EXCEPTION')
            if mode=='size':return httpx.Response(200,content=b'x'*(provider.MAX_RESPONSE_BYTES+1))
            content='{} PRIVATE_BODY' if mode=='syntax' else '{}' if mode=='schema' else json.dumps(second() if is_stage2 else first())
        message={'role':'assistant','content':content,'reasoning_content':'PRIVATE_HIDDEN_TRACE'}
        if len(calls)==stage:
            if mode=='envelope':message=[]
            if mode=='refusal':message['refusal']='PRIVATE_REFUSAL'
            if mode=='tool':message['tool_calls']=[{'id':'PRIVATE_TOOL'}]
        return httpx.Response(200,json={'choices':[{'finish_reason':'length' if len(calls)==stage and mode=='finish' else 'stop','message':message}]})
    transport=httpx.MockTransport(handler)
    reasoner=orchestration.TwoStageReasoner(provider.StageService('stage_1',transport),provider.StageService('stage_2',transport))
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),reasoner))
    row=report['outcomes'][0]
    assert len(calls)==stage
    assert row['failure_stage']==f'stage_{stage}'
    assert (row['error'],row['invalid_reason'],row['json_reason'],row['schema_reason'],row['failure_kind'],row['http_status'])==(error,invalid,json_reason,schema,kind,status)
    assert row['predicted'] is None and not row['mapped_action_correct']
    assert report['metrics']['action_match_rate']==0
    assert 'PRIVATE' not in json.dumps(report)
    assert 'reason' not in row and 'next_prompt' not in row
    assert row['stages'][f'stage_{stage}']['valid'] is False
    if stage==2:assert row['stages']['stage_1']['valid'] is True


def test_unavailable_stage1_no_stage2():
    reasoner=orchestration.TwoStageReasoner()
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),reasoner))
    row=report['outcomes'][0]
    assert row['failure_stage']=='stage_1' and row['failure_kind']=='unavailable'
    assert not row['stages']['stage_2']['attempted']


def test_actual_gate_not_expected_oracle():
    # Reviewed blocking=true case deliberately receives a valid false gate.
    one,two=Fake(first()),Fake(second())
    reasoner=orchestration.TwoStageReasoner(one,two)
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['blocking_context-001']),reasoner))
    row=report['outcomes'][0]
    assert row['predicted']=='CHALLENGE' and row['expected']=='FOLLOW_UP'
    assert len(two.contexts)==1 and row['unexpected_stage_2']
    assert report['stage_metrics']['stage_2']['unexpected_visits']==1
    assert report['stage_metrics']['stage_2']['semantics']['unresolved_reasoning_issue']['correct']==1
    # Reviewed continuation case deliberately receives a valid true gate.
    one,two=Fake(first(True,True,'Missing?')),Fake(error=AssertionError('Oracle routing forbidden'))
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),orchestration.TwoStageReasoner(one,two)))
    assert not two.contexts and report['outcomes'][0]['predicted']=='FOLLOW_UP'
    assert report['stage_metrics']['stage_2']['expected_route_coverage']=={'attempted':0,'valid':0,'expected':1}


def test_provider_success_context_and_settings(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY','PRIVATE_KEY');calls=[]
    def handler(req):
        sent=json.loads(req.content);calls.append(sent)
        result=first() if len(calls)==1 else second()
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':json.dumps(result),'reasoning_content':'PRIVATE_TRACE'}}]})
    transport=httpx.MockTransport(handler)
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),orchestration.TwoStageReasoner(provider.StageService('stage_1',transport),provider.StageService('stage_2',transport))))
    assert len(calls)==2 and report['stage_call_attempts']==2
    assert calls[0]['messages'][1]==calls[1]['messages'][1]
    assert 'PRIVATE_STAGE1_REASON' not in json.dumps(calls[1])
    assert report['outcomes'][0]['predicted']=='CHALLENGE'
    assert 'PRIVATE' not in json.dumps(report)
    for sent in calls:
        assert set(sent)=={'model','stream','messages',*asdict(provider.ASSESSMENT_CONFIG)}
        assert {k:sent[k] for k in asdict(provider.ASSESSMENT_CONFIG)}==dict(temperature=1.0,top_p=.95,max_tokens=1024,reasoning_effort='high',reasoning_budget=256)
        assert sent['model']=='nvidia/nemotron-3-super-120b-a12b' and sent['stream'] is False
    assert provider.PROVIDER_TIMEOUT_SECONDS==orchestration.COMBINED_DEADLINE_SECONDS==30


def test_stage_prompts_general_and_separate():
    assert reviewed.BLOCKING_CONTEXT_DEFINITION in provider.STAGE1_INSTRUCTIONS
    assert 'Return exactly understandable_relevant, blocking_context_gap, reason, next_prompt.' in provider.STAGE1_INSTRUCTIONS
    assert 'Do not assess unresolved reasoning issues in this stage.' in provider.STAGE1_INSTRUCTIONS
    assert 'Return exactly unresolved_reasoning_issue, reason, next_prompt.' in provider.STAGE2_INSTRUCTIONS
    for prompt in (provider.STAGE1_INSTRUCTIONS,provider.STAGE2_INSTRUCTIONS):
        for text in ('current_prompt','prior_turns','embedded commands','application-level','not private chain-of-thought'):assert text in prompt
        for cid in reviewed.REVIEWED_IDS:assert cid not in prompt
    assert provider.PROMPT_VERSION=='interviewer-two-stage-v1'


def test_reviewed_material_and_guards():
    cases=reviewed.select_development_cases([*reviewed.ORIGINAL_IDS,*reviewed.BOUNDARY_IDS])
    expected=reviewed.reviewed_expectations(cases)
    assert len(cases)==11 and sum(x.understandable_relevant and x.blocking_context_gap is False for x in expected)==6
    class Never:
        async def decide(self,ctx):pytest.fail('Provider called')
    for invalid in ([],[cases[0].model_copy(update={'split':'held_out'})],[cases[0]]*2):
        with pytest.raises(ValueError):asyncio.run(live.experiment_report(invalid,Never()))


@pytest.mark.parametrize('argv',[
    ['two_stage','--case-id','unsupported_claim-003'],
    ['two_stage','--live','--case-id','unknown-001'],
    ['two_stage','--live'],
    ['two_stage','--live','--case-id','unsupported_claim-003','--split','held_out']])
def test_cli_guards_before_provider_construction(monkeypatch,argv):
    monkeypatch.setattr(sys,'argv',argv)
    monkeypatch.setattr(live,'TwoStageReasoner',lambda:pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit):live.main()

@pytest.mark.parametrize('stage',[1,2])
def test_request_and_content_limits(monkeypatch,stage):
    monkeypatch.setenv('NVIDIA_API_KEY','PRIVATE_KEY')
    def forbidden(req):pytest.fail('Request-size rejection must occur before transport')
    monkeypatch.setattr(provider,'MAX_REQUEST_BYTES',1)
    service=provider.StageService(f'stage_{stage}',httpx.MockTransport(forbidden))
    with pytest.raises(ReasoningFailed) as caught:asyncio.run(service.decide(context()))
    assert caught.value.failure.failure_kind=='request_size'
    assert caught.value.failure.http_status is None


def test_budget_expiration_during_mapping_has_no_action(monkeypatch):
    clock=Clock();original=orchestration.map_stage2
    def mapping(result):
        decision=original(result);clock.value=31;return decision
    monkeypatch.setattr(orchestration,'map_stage2',mapping)
    reasoner=orchestration.TwoStageReasoner(Fake(first()),Fake(second()),clock=clock)
    with pytest.raises(ReasoningTimeout):asyncio.run(reasoner.decide(context()))
    assert reasoner.last_trace.failure_stage=='stage_2'


def test_unexpected_stage2_on_nonassessable_review_not_scored():
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['irrelevant-002']),
        orchestration.TwoStageReasoner(Fake(first()),Fake(second(False,None)))))
    assert report['outcomes'][0]['predicted']=='MOVE_ON'
    assert not report['outcomes'][0]['mapped_action_correct']
    assert report['outcomes'][0]['stages']['stage_2']['assessments']['unresolved_reasoning_issue']=={
        'expected':None,'predicted':False,'applicable':False,'correct':None}
    assert report['stage_metrics']['stage_2']['unexpected_visits']==1
    assert report['stage_metrics']['stage_2']['semantics']['unresolved_reasoning_issue']['applicable']==0


def test_exhausted_stage2_budget_report_has_safe_stage_and_no_call(monkeypatch):
    clock=Clock();original=orchestration.route_stage1
    def route(value):
        answer=original(value);clock.value=31;return answer
    monkeypatch.setattr(orchestration,'route_stage1',route)
    one,two=Fake(first()),Fake(second())
    reasoner=orchestration.TwoStageReasoner(one,two,clock=clock)
    report=asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),reasoner))
    row=report['outcomes'][0]
    assert row['error']=='timeout' and row['failure_stage']=='stage_2'
    assert row['predicted'] is None and not two.contexts
    assert report['stage_call_attempts']==1
    assert report['stage_metrics']['stage_2']['attempted']==0
    assert report['stage_metrics']['stage_2']['timeouts']==1


# Pre-implementation hashes freeze production, all earlier evaluators and reviewed material.
HISTORICAL_HASHES = {'backend/app/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'backend/app/audio.py': 'd7d1ef90dd893a80131e2bd9a9e2a5c2df72085f72ce140d63cc99e4e62aa3fa', 'backend/app/interview_orchestration.py': '996f6a538c61bd2dcec544866727512b71a809f7f31a05b0b88751bf1ef99d71', 'backend/app/main.py': '0310c563690a24669ca3bfcaf461617ff7bd20b2da3e5432d20b06e1d9fb77b4', 'backend/app/nemotron.py': '3a5157713ad94650b83d86d0b18f37c48c57f93b04f7ebcfa72843d121d4c604', 'backend/app/reasoning.py': '04df966e60ff9b815b2154e88de55ecda74c6967ac8df831374ec614371c14bc', 'backend/app/session_routes.py': 'b3c2ae058e0ac4db9583923b9ab02e94041334ba61bc4205604001bebbf8e376', 'backend/app/sessions.py': 'a833b66a1191171a5ae335e1227f7ca871e616eabc106949adee190286218739', 'backend/app/transcription.py': '47137d467f96022195b750c3a01c2ce9c56a751ee9cec580835bef6aeea6e86e', 'evals/interviewer/ASSESSMENT.md': '2b551bc8f49463e47509730bcaca64c38e383f970b610ff225db7ed3d819120c', 'evals/interviewer/ASSESSMENT_ONLY.md': 'ac355db6fda39eee8f4ba644bb54d86e060f66c802daf07924bb026c79c7275f', 'evals/interviewer/ASSESSMENT_V2.md': '3c2accddb5fc01a3d65b20bbd3639fc07ed405e925c93657a85123bb4045e077', 'evals/interviewer/BLOCKING_CONTEXT.md': 'ea2557bae4c33c4d27020547eca34e29ec2302c061b946f0aa2e1330aed29370', 'evals/interviewer/README.md': '5144fa63602e53f7f58fed5db88b797b9fb975d54c1e4ba6a91998041181c591', 'evals/interviewer/assessment_independent.py': 'e0bed7d6e1d85ae462e6ce2fd622897481236366bc56700a7ac17ce1a3e96f4f', 'evals/interviewer/assessment_live.py': '8673c8548cfe10a9b167ce7b8d2dd4327dbb1c9686bdbf7e88e3bdfe772bcf6e', 'evals/interviewer/assessment_live_v2.py': '5075fb91e646e0179a761560200eaa5a698c532da29f159d670d2fe52166123c', 'evals/interviewer/assessment_live_v3.py': '4af0dddf5b3f18863d188df2cccce3e9ef93e71cdaa2626bd5b48e845777daf2', 'evals/interviewer/assessment_only.py': '2a938d23f44ad180fc8c5c09d47aadd99822916de1f17848510ed95668f9ef90', 'evals/interviewer/assessment_only_live.py': 'a7b4717717784e1bd2e0889ff70105968e3c713741c40a8758bf1e48aba6538f', 'evals/interviewer/assessment_prototype.py': '1c8aadb9915ff968086b8c107cd909b6d2a367cec64db2399ad8ca51e729cf9e', 'evals/interviewer/blocking_context.py': '5a2ac6d06547cf70d85bb48c70c09126ff6d834c93d03ce1c6b2557f695a5129', 'evals/interviewer/blocking_context_annotations_development.json': '4882d3d139889e21f7b80d99e723cc7a00ca70a70b7c662d844a54cb4d2fdac5', 'evals/interviewer/blocking_context_boundary_cases_development.json': '70a9bf3e23991a02843f5075aaaf94a4332ac09daede1841e6790e11958c7956', 'evals/interviewer/blocking_context_live.py': '45f184326f0fa2aad37f95869d73d8bfa89f47a8fe070d0da8c9389b40bdc942', 'evals/interviewer/cases.jsonl': 'd496391a3f25ac8173e504794497c4aa4120526a0c7238f94164b3bd234dfc35', 'evals/interviewer/evaluate.py': '3c6f4db841cd8df3e72b5211f430bb807a208c22221efbc14c0fba9557975309', 'evals/interviewer/independent_annotations_development_draft.json': '24723e8e24ab6d025709b9e59a09e122c7fc390af20d8ff250487960f8ed350f'}

def test_historical_production_and_annotations_unchanged():
    for path, digest in HISTORICAL_HASHES.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path

def test_production_has_no_experimental_imports():
    for path in Path("backend/app").glob("*.py"):
        assert "two_stage" not in path.read_text()

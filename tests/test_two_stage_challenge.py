"""Offline contracts/boundaries and frozen-operation reuse; never model intelligence."""
import asyncio
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.reasoning import (Decision, InvalidDecision, ProviderFailure, ReasoningContext,
                           ReasoningFailed, ReasoningTimeout, ReasoningUnavailable)
from app.sessions import AnswerRequest, InterviewSessionService, SessionConflict
from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer import two_stage_contract as historical_contract
from evals.interviewer import two_stage_orchestration as historical_operation
from evals.interviewer import two_stage_provider as historical_provider
from evals.interviewer import two_stage_challenge_contract as contract
from evals.interviewer import two_stage_challenge_orchestration as orchestration
from evals.interviewer import two_stage_challenge_provider as provider
from evals.interviewer import two_stage_challenge_live as live


def first(u=True, gap=False, prompt=None):
    return dict(understandable_relevant=u, blocking_context_gap=gap,
                reason='PRIVATE_STAGE1_REASON', next_prompt=prompt)


def second(warranted=True, prompt='PRIVATE_STAGE2_PROMPT', **updates):
    value = dict(challenge_warranted=warranted, reason='PRIVATE_STAGE2_REASON',
                 next_prompt=prompt)
    value.update(updates)
    return value


def context():
    return ReasoningContext(question='PRIVATE_QUESTION', current_prompt='PRIVATE_CURRENT',
                            answer='PRIVATE_ANSWER')


class Fake:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.contexts = value, error, []

    async def decide(self, ctx):
        self.contexts.append(ctx)
        if self.error:
            raise self.error
        return self.value


@pytest.mark.parametrize('warranted,prompt', list(itertools.product(
    (True, False, None, 0, 1, 'true', 'false'), (None, 'Probe'))))
def test_strict_boolean_and_prompt_matrix(warranted, prompt):
    if type(warranted) is bool and warranted == (prompt is not None):
        value = contract.parse_stage2(json.dumps(second(warranted, prompt)))
        assert contract.map_stage2(value).action == ('CHALLENGE' if warranted else 'MOVE_ON')
    else:
        with pytest.raises(contract.InvalidAssessment) as caught:
            contract.parse_stage2(json.dumps(second(warranted, prompt)))
        assert caught.value.invalid_reason == 'schema_validation'
        assert caught.value.schema_reason == ('assessment_prompt_inconsistency'
                                              if type(warranted) is bool else 'strict_type_violation')
        assert str(caught.value) == ''


def test_exact_schema_and_frozen_revalidation():
    schema = contract.ChallengeStage2.model_json_schema()
    fields = {'challenge_warranted', 'reason', 'next_prompt'}
    assert set(schema['properties']) == set(schema['required']) == fields
    assert schema['additionalProperties'] is False
    assert schema['properties']['challenge_warranted']['type'] == 'boolean'
    assert schema['properties']['reason']['minLength'] == 1
    assert schema['properties']['reason']['maxLength'] == 300
    prompt = schema['properties']['next_prompt']['anyOf']
    assert prompt == [{'maxLength': 500, 'minLength': 1, 'type': 'string'}, {'type': 'null'}]
    valid = contract.validate_stage2(second())
    with pytest.raises(ValueError):
        valid.challenge_warranted = False
    forged = contract.ChallengeStage2.model_construct(**second(False, 'Probe'))
    with pytest.raises(InvalidDecision):
        contract.map_stage2(forged)


@pytest.mark.parametrize('field', ['challenge_warranted', 'reason', 'next_prompt'])
def test_missing_fields(field):
    value = second()
    value.pop(field)
    with pytest.raises(contract.InvalidAssessment) as caught:
        contract.validate_stage2(value)
    assert caught.value.schema_reason == 'missing_required_field'


@pytest.mark.parametrize('field', ['unresolved_reasoning_issue', 'action', 'action_label',
    'understandable_relevant', 'blocking_context_gap', 'issue_priority', 'score', 'grade', 'other'])
def test_forbidden_fields(field):
    with pytest.raises(contract.InvalidAssessment) as caught:
        contract.validate_stage2(second(**{field: True}))
    assert caught.value.schema_reason == 'forbidden_extra_field'


@pytest.mark.parametrize('field,value,code', [
    ('reason', None, 'strict_type_violation'), ('reason', 1, 'strict_type_violation'),
    ('reason', '', 'text_bound_violation'), ('reason', ' ', 'text_bound_violation'),
    ('reason', 'x'*301, 'text_bound_violation'), ('next_prompt', 1, 'strict_type_violation'),
    ('next_prompt', '', 'text_bound_violation'), ('next_prompt', ' ', 'text_bound_violation'),
    ('next_prompt', 'x'*501, 'text_bound_violation')])
def test_text_rejections(field, value, code):
    with pytest.raises(contract.InvalidAssessment) as caught:
        contract.validate_stage2(second(**{field: value}))
    assert caught.value.schema_reason == code and str(caught.value) == ''


@pytest.mark.parametrize('reason,prompt', [(' r ', ' p '), ('r'*300, 'p'*500)])
def test_valid_trimmed_boundaries(reason, prompt):
    value = contract.validate_stage2(second(reason=reason, next_prompt=prompt))
    assert value.reason == reason.strip() and value.next_prompt == prompt.strip()


@pytest.mark.parametrize('content,invalid,json_reason', [
    (' ', 'json_syntax', 'empty_content'), ('{} PRIVATE', 'json_syntax', 'trailing_data'),
    ('{"x":', 'json_syntax', 'json_error_at_end'), ('PRIVATE {}', 'json_syntax', 'other_json_syntax'),
    ('```json\n{}\n```', 'json_syntax', 'other_json_syntax'),
    ('\ud800', 'json_syntax', 'other_parse_failure'),
    ('{"challenge_warranted":true,"challenge_warranted":false}', 'duplicate_json_key', None),
    ('NaN', 'non_json_constant', None), ('Infinity', 'non_json_constant', None),
    (None, 'content_type', None), (' '*8193, 'content_size', None)])
def test_strict_shared_parser_no_recovery(content, invalid, json_reason):
    with pytest.raises(InvalidDecision) as caught:
        contract.parse_stage2(content)
    assert (caught.value.invalid_reason, caught.value.json_reason) == (invalid, json_reason)
    assert str(caught.value) == ''


def test_reuses_operation_without_reimplementing_or_mutating_it():
    assert orchestration.ChallengeReasoner.decide is historical_operation.TwoStageReasoner.decide
    assert orchestration.ChallengeReasoner._stage is historical_operation.TwoStageReasoner._stage
    assert orchestration.submit_experimental is historical_operation.submit_experimental
    assert provider.ChallengeStageService._request is historical_provider.StageService._request
    assert provider.ChallengeStageService.decide is historical_provider.StageService.decide
    reasoner = orchestration.ChallengeReasoner()
    assert type(reasoner.stage1) is historical_provider.StageService
    assert reasoner.stage1.instructions == historical_provider.STAGE1_INSTRUCTIONS
    assert reasoner.stage1.parse_content is historical_contract.parse_stage1
    assert reasoner.stage2.provider.parse_content is contract.parse_stage2
    assert reasoner.stage2.provider.instructions == provider.STAGE2_INSTRUCTIONS
    assert historical_operation.COMBINED_DEADLINE_SECONDS == 30


@pytest.mark.parametrize('value,action', [(first(False, None, 'Meaning?'), 'CLARIFY'),
                                        (first(True, True, 'Result?'), 'FOLLOW_UP')])
def test_terminal_stage1_skips_stage2(value, action):
    one, two = Fake(value), Fake(error=AssertionError('Must not call Stage 2'))
    reasoner = orchestration.ChallengeReasoner(one, two)
    assert asyncio.run(reasoner.decide(context())).action == action
    assert len(one.contexts) == 1 and not two.contexts


@pytest.mark.parametrize('signal,action', [(True, 'CHALLENGE'), (False, 'MOVE_ON')])
def test_actual_continue_exactly_once_and_no_stage1_text(signal, action):
    one, two = Fake(first()), Fake(second(signal, 'Probe' if signal else None))
    reasoner = orchestration.ChallengeReasoner(one, two)
    decision = asyncio.run(reasoner.decide(context()))
    assert decision.action == action
    assert decision == contract.map_stage2(contract.validate_stage2(two.value))
    assert len(one.contexts) == len(two.contexts) == 1
    assert one.contexts[0] is two.contexts[0]
    assert 'PRIVATE_STAGE1_REASON' not in two.contexts[0].model_dump_json()


def test_legacy_stage2_output_rejected_by_new_adapter():
    two = Fake(dict(unresolved_reasoning_issue=True, reason='old', next_prompt='Probe'))
    reasoner = orchestration.ChallengeReasoner(Fake(first()), two)
    with pytest.raises(contract.InvalidAssessment) as caught:
        asyncio.run(reasoner.decide(context()))
    assert caught.value.schema_reason == 'missing_required_field'
    assert len(two.contexts) == 1 and reasoner.last_trace.failure_stage == 'stage_2'


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('failure', [InvalidDecision(invalid_reason='json_syntax',
    json_reason='other_json_syntax'), ReasoningTimeout(), ReasoningUnavailable(),
    ReasoningFailed(failure=ProviderFailure(failure_kind='transport'))])
def test_failure_has_no_action_no_retry(stage, failure):
    one = Fake(first(), failure if stage == 1 else None)
    two = Fake(second(), failure if stage == 2 else None)
    reasoner = orchestration.ChallengeReasoner(one, two)
    with pytest.raises(type(failure)):
        asyncio.run(reasoner.decide(context()))
    assert len(one.contexts) == 1 and len(two.contexts) == (stage == 2)
    assert reasoner.last_trace.failure_stage == f'stage_{stage}'


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


def test_one_deadline_stage2_receives_only_remainder(monkeypatch):
    clock, budgets = Clock(), []

    async def wait(awaitable, timeout):
        budgets.append(timeout)
        result = await awaitable
        clock.value += 7 if len(budgets) == 1 else 4
        return result

    monkeypatch.setattr(historical_operation, 'wait_for', wait)
    reasoner = orchestration.ChallengeReasoner(Fake(first()), Fake(second()), clock=clock)
    assert asyncio.run(reasoner.decide(context())).action == 'CHALLENGE'
    assert budgets == [30, 23] and reasoner.last_trace.latency_ms == 11000


def test_exhaustion_before_second_call_reports_timeout_without_call(monkeypatch):
    clock, original = Clock(), historical_operation.route_stage1

    def route(value):
        result = original(value)
        clock.value = 31
        return result

    monkeypatch.setattr(historical_operation, 'route_stage1', route)
    two = Fake(second())
    report = asyncio.run(live.experiment_report(
        reviewed.select_development_cases(['unsupported_claim-003']),
        orchestration.ChallengeReasoner(Fake(first()), two, clock=clock)))
    row = report['outcomes'][0]
    assert not two.contexts and row['predicted'] is None
    assert row['error'] == 'timeout' and row['failure_stage'] == 'stage_2'
    assert report['stage_call_attempts'] == 1
    assert report['stage_metrics']['stage_2']['attempted'] == 0
    assert report['stage_metrics']['stage_2']['timeouts'] == 1


def test_shared_timeout_cancels_second_call(monkeypatch):
    monkeypatch.setattr(historical_operation, 'COMBINED_DEADLINE_SECONDS', .025)

    class Waiting(Fake):
        cancelled = False

        async def decide(self, ctx):
            self.contexts.append(ctx)
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    two = Waiting()
    reasoner = orchestration.ChallengeReasoner(Fake(first()), two)
    with pytest.raises(ReasoningTimeout):
        asyncio.run(reasoner.decide(context()))
    assert two.cancelled and len(two.contexts) == 1
    assert reasoner.last_trace.failure_stage == 'stage_2'


def request(session, text='Draft', submission_id=None):
    return AnswerRequest(question_index=session.current_question_index,
                         turn_revision=session.turn_revision,
                         submission_id=submission_id or uuid4(), answer=text)


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('failure', [InvalidDecision(invalid_reason='json_syntax'),
                                    ReasoningTimeout(), ReasoningFailed()])
def test_failure_atomicity_preserves_draft_and_state(stage, failure):
    sessions = InterviewSessionService()
    session = sessions.start()
    answer = request(session)
    reasoner = orchestration.ChallengeReasoner(
        Fake(first(), failure if stage == 1 else None),
        Fake(second(), failure if stage == 2 else None))
    with pytest.raises(type(failure)):
        asyncio.run(orchestration.submit_experimental(sessions, session.id, answer, reasoner))
    assert sessions.get(session.id) == session and session.id not in sessions._pending
    replacement = orchestration.ChallengeReasoner(Fake(first()), Fake(second(False, None)))
    updated = asyncio.run(orchestration.submit_experimental(sessions, session.id, answer, replacement))
    assert updated.turn_revision == 1 and updated.answers == ['Draft']


@pytest.mark.parametrize('stage', [1, 2])
def test_stale_result_after_either_stage_is_discarded(stage):
    sessions = InterviewSessionService()
    session = sessions.start()

    class Stale(Fake):
        async def decide(self, ctx):
            result = await super().decide(ctx)
            with sessions._lock:
                sessions._sessions[session.id].turn_revision += 1
            return result

    one = Stale(first()) if stage == 1 else Fake(first())
    two = Stale(second()) if stage == 2 else Fake(second())
    reasoner = orchestration.ChallengeReasoner(one, two)
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions, session.id, request(session), reasoner))
    current = sessions.get(session.id)
    assert current.answers == [] and current.turns == [] and current.probe_count == 0
    assert current.turn_revision == 1 and session.id not in sessions._pending
    assert len(two.contexts) == (stage == 2)


def test_duplicate_replay_conflict_and_probe_limit_have_no_extra_calls():
    sessions = InterviewSessionService()
    session = sessions.start()
    answer = request(session)
    one, two = Fake(first()), Fake(second(False, None))
    reasoner = orchestration.ChallengeReasoner(one, two)
    updated = asyncio.run(orchestration.submit_experimental(sessions, session.id, answer, reasoner))
    assert asyncio.run(orchestration.submit_experimental(sessions, session.id, answer, reasoner)) == updated
    assert len(one.contexts) == len(two.contexts) == 1
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions, session.id,
            answer.model_copy(update={'answer': 'Changed'}), reasoner))
    probes, unused = Fake(first(True, True, 'Result?')), Fake(error=AssertionError('Forbidden'))
    reasoner = orchestration.ChallengeReasoner(probes, unused)
    for _ in range(3):
        updated = asyncio.run(orchestration.submit_experimental(sessions, session.id,
                                                              request(updated), reasoner))
    assert len(probes.contexts) == 2 and not unused.contexts
    assert updated.turns[-1].transition_source == 'probe_limit'
    assert updated.turns[-1].action is None


def test_true_challenge_recommendation_cannot_override_probe_budget():
    sessions = InterviewSessionService()
    current = sessions.start()
    one, two = Fake(first()), Fake(second())
    reasoner = orchestration.ChallengeReasoner(one, two)
    for _ in range(3):
        current = asyncio.run(orchestration.submit_experimental(
            sessions, current.id, request(current), reasoner))
    assert len(one.contexts) == len(two.contexts) == 2
    assert [turn.action for turn in current.turns[:2]] == ['CHALLENGE', 'CHALLENGE']
    assert current.turns[-1].transition_source == 'probe_limit'
    assert current.turns[-1].action is None and current.current_question_index == 1


@pytest.mark.parametrize('stage', [1, 2])
def test_no_lock_across_await_and_cancellation_preserves_state(stage):
    async def scenario():
        sessions = InterviewSessionService()
        session = sessions.start()
        entered = asyncio.Event()

        class Waiting(Fake):
            async def decide(self, ctx):
                self.contexts.append(ctx)
                entered.set()
                await asyncio.Event().wait()

        one = Waiting() if stage == 1 else Fake(first())
        two = Waiting() if stage == 2 else Fake(second())
        reasoner = orchestration.ChallengeReasoner(one, two)
        answer = request(session)
        task = asyncio.create_task(orchestration.submit_experimental(sessions, session.id, answer, reasoner))
        await entered.wait()
        assert sessions._lock.acquire(blocking=False)
        sessions._lock.release()
        assert sessions.get(session.id) == session
        with pytest.raises(SessionConflict):
            await orchestration.submit_experimental(sessions, session.id, answer, reasoner)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions.get(session.id) == session and session.id not in sessions._pending
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('mode,error,invalid,json_reason,schema,kind,status', [
    ('syntax', 'invalid_output', 'json_syntax', 'trailing_data', None, None, None),
    ('schema', 'invalid_output', 'schema_validation', None, 'missing_required_field', None, None),
    ('envelope', 'invalid_output', 'response_envelope', None, None, None, None),
    ('finish', 'invalid_output', 'finish_reason', None, None, None, None),
    ('tool', 'invalid_output', 'tool_or_function_call', None, None, None, None),
    ('refusal', 'invalid_output', 'refusal', None, None, None, None),
    ('content_type', 'invalid_output', 'content_type', None, None, None, None),
    ('content_size', 'invalid_output', 'content_size', None, None, None, None),
    ('response_size', 'invalid_output', 'response_size', None, None, None, None),
    ('http', 'provider_error', None, None, None, 'http_status', 429),
    ('transport', 'provider_error', None, None, None, 'transport', None),
    ('timeout', 'timeout', None, None, None, None, None),
    ('adapter', 'provider_error', None, None, None, 'adapter_error', None)])
def test_mock_transport_diagnostics_privacy_and_no_retry(monkeypatch, caplog, capsys,
    stage, mode, error, invalid, json_reason, schema, kind, status):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == stage:
            if mode == 'http':
                return httpx.Response(429, text='PRIVATE_BODY')
            if mode == 'transport':
                raise httpx.ConnectError('PRIVATE_EXCEPTION')
            if mode == 'timeout':
                raise httpx.ReadTimeout('PRIVATE_EXCEPTION')
            if mode == 'adapter':
                raise RuntimeError('PRIVATE_EXCEPTION')
            if mode == 'response_size':
                return httpx.Response(200, content=b'x'*(historical_provider.MAX_RESPONSE_BYTES+1))
        content = json.dumps(first() if len(calls) == 1 else second())
        if len(calls) == stage:
            if mode == 'syntax':
                content = '{} PRIVATE_BODY'
            if mode == 'schema':
                content = '{}'
            if mode == 'content_type':
                content = None
            if mode == 'content_size':
                content = ' '*8193
        message = {'role': 'assistant', 'content': content, 'reasoning_content': 'PRIVATE_HIDDEN_TRACE'}
        if len(calls) == stage:
            if mode == 'envelope':
                message = []
            if mode == 'tool':
                message['tool_calls'] = [{'id': 'PRIVATE_TOOL'}]
            if mode == 'refusal':
                message['refusal'] = 'PRIVATE_REFUSAL'
        return httpx.Response(200, json={'choices': [{
            'finish_reason': 'length' if len(calls) == stage and mode == 'finish' else 'stop',
            'message': message}]})

    transport = httpx.MockTransport(handler)
    reasoner = orchestration.ChallengeReasoner(
        historical_provider.StageService('stage_1', transport), provider.ChallengeStageService(transport))
    report = asyncio.run(live.experiment_report(
        reviewed.select_development_cases(['unsupported_claim-003']), reasoner))
    row = report['outcomes'][0]
    assert len(calls) == stage and row['failure_stage'] == f'stage_{stage}'
    assert (row['error'], row['invalid_reason'], row['json_reason'], row['schema_reason'],
            row['failure_kind'], row['http_status']) == (error, invalid, json_reason, schema, kind, status)
    assert row['predicted'] is None and not row['mapped_action_correct']
    assert report['metrics']['action_match_rate'] == 0
    assert 'PRIVATE' not in json.dumps(report) + caplog.text + capsys.readouterr().out
    assert 'reason' not in row and 'next_prompt' not in row
    for observation in row['stages'].values():
        assert 'reason' not in observation and 'next_prompt' not in observation
    assert 'unresolved_reasoning_issue' not in json.dumps(report)
    assert not row['stages'][f'stage_{stage}']['valid']


def test_success_transport_payload_settings_and_report_allowlist(monkeypatch, caplog, capsys):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []

    def handler(req):
        sent = json.loads(req.content)
        calls.append(sent)
        value = first() if len(calls) == 1 else second()
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(value), 'reasoning_content': 'PRIVATE_HIDDEN'}}]})

    transport = httpx.MockTransport(handler)
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),
        orchestration.ChallengeReasoner(historical_provider.StageService('stage_1', transport),
                                       provider.ChallengeStageService(transport))))
    assert len(calls) == report['stage_call_attempts'] == 2
    assert calls[0]['messages'][0]['content'] == historical_provider.STAGE1_INSTRUCTIONS
    assert calls[1]['messages'][0]['content'] == provider.STAGE2_INSTRUCTIONS
    assert calls[0]['messages'][1] == calls[1]['messages'][1]
    assert 'PRIVATE_STAGE1_REASON' not in json.dumps(calls[1])
    for sent in calls:
        assert set(sent) == {'model', 'stream', 'messages', *asdict(historical_provider.ASSESSMENT_CONFIG)}
        assert {key: sent[key] for key in asdict(historical_provider.ASSESSMENT_CONFIG)} == dict(
            temperature=1.0, top_p=.95, max_tokens=1024, reasoning_effort='high', reasoning_budget=256)
        assert sent['stream'] is False and sent['model'] == 'nvidia/nemotron-3-super-120b-a12b'
    assert report['prompt_version'] == provider.PROMPT_VERSION == 'interviewer-two-stage-challenge-v1'
    assert report['stage_1_prompt_version'] == 'interviewer-two-stage-v1'
    row = report['outcomes'][0]
    assert row['predicted'] == 'CHALLENGE'
    assert row['stages']['stage_2']['challenge_warranted'] is True
    assert report['stage_metrics']['stage_2']['semantics']['challenge_warranted']['correct'] == 1
    assert 'PRIVATE' not in json.dumps(report) + caplog.text + capsys.readouterr().out
    assert 'unresolved_reasoning_issue' not in json.dumps(report)
    second_fields = {'attempted', 'valid', 'understandable_relevant', 'blocking_context_gap',
        'challenge_warranted', 'error', 'invalid_reason', 'json_reason', 'schema_reason',
        'failure_kind', 'http_status', 'latency_ms', 'assessments'}
    assert set(row['stages']['stage_2']) == second_fields
    assert set(row['stages']['stage_2']['assessments']) == {'challenge_warranted'}
    assert all(row[name] is None for name in ('error', 'invalid_reason', 'json_reason',
                                            'schema_reason', 'failure_kind', 'http_status'))


def test_actual_gate_never_uses_new_annotations_as_oracle():
    one, two = Fake(first()), Fake(second())
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['blocking_context-001']),
                                               orchestration.ChallengeReasoner(one, two)))
    row = report['outcomes'][0]
    assert len(two.contexts) == 1 and row['unexpected_stage_2']
    assert row['expected'] == 'FOLLOW_UP' and row['predicted'] == 'CHALLENGE'
    assert row['stages']['stage_2']['assessments']['challenge_warranted'] == {
        'expected': None, 'predicted': True, 'correct': None, 'applicable': False}
    assert report['stage_metrics']['stage_2']['semantics']['challenge_warranted']['applicable'] == 0
    two = Fake(error=AssertionError('Do not use annotation oracle'))
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),
        orchestration.ChallengeReasoner(Fake(first(True, True, 'Missing?')), two)))
    assert not two.contexts and report['outcomes'][0]['predicted'] == 'FOLLOW_UP'
    assert report['stage_metrics']['stage_2']['expected_route_coverage'] == {
        'attempted': 0, 'valid': 0, 'expected': 1}


def test_irrelevant_unexpected_visit_is_not_assessable():
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['irrelevant-002']),
        orchestration.ChallengeReasoner(Fake(first()), Fake(second(False, None)))))
    row = report['outcomes'][0]
    assert row['unexpected_stage_2'] and not row['mapped_action_correct']
    assert row['stages']['stage_2']['assessments']['challenge_warranted']['applicable'] is False


def test_missing_configuration_and_request_size_preserve_diagnostics(monkeypatch):
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),
                                               orchestration.ChallengeReasoner()))
    assert report['outcomes'][0]['failure_kind'] == 'unavailable'
    assert not report['outcomes'][0]['stages']['stage_2']['attempted']
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    monkeypatch.setattr(historical_provider, 'MAX_REQUEST_BYTES', 1)
    def forbidden(req):
        pytest.fail('Request-size rejection must precede transport')
    service = provider.ChallengeStageService(httpx.MockTransport(forbidden))
    with pytest.raises(ReasoningFailed) as caught:
        asyncio.run(service.decide(context()))
    assert caught.value.failure.failure_kind == 'request_size'


@pytest.mark.parametrize('argv', [
    ['challenge', '--case-id', 'unsupported_claim-003'], ['challenge', '--live'],
    ['challenge', '--live', '--case-id', 'unknown-001'],
    ['challenge', '--live', '--case-id', 'unsupported_claim-003', 'unsupported_claim-003'],
    ['challenge', '--live', '--case-id', 'unsupported_claim-003', '--split', 'held_out']])
def test_cli_guards_before_provider_construction(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(live, 'ChallengeReasoner', lambda: pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit):
        live.main()


@pytest.mark.parametrize('tamper', ['held_out', 'duplicate', 'empty', 'source', 'annotation'])
def test_source_review_and_annotation_guards_before_calls(monkeypatch, tmp_path, tamper):
    cases = reviewed.select_development_cases(['unsupported_claim-003'])
    if tamper == 'held_out':
        cases = [cases[0].model_copy(update={'split': 'held_out'})]
    if tamper == 'duplicate':
        cases = cases*2
    if tamper == 'empty':
        cases = []
    if tamper == 'source':
        cases = [cases[0].model_copy(update={'answer': 'Changed'})]
    if tamper == 'annotation':
        path = tmp_path/'review.json'
        path.write_text('{}')
        monkeypatch.setattr(live, 'ANNOTATIONS', path)
    class Never:
        async def decide(self, ctx):
            pytest.fail('Provider called')
    with pytest.raises(ValueError):
        asyncio.run(live.experiment_report(cases, Never()))


def test_prompt_definition_and_contract_general_not_case_specific():
    prompt = provider.STAGE2_INSTRUCTIONS
    assert provider.CHALLENGE_DEFINITION in prompt
    for clause in ('important interview\nvalue now', 'material evidential leap',
        'overgeneralized\nconclusion', "decision's assumptions, consequences, costs, or alternatives",
        'optional detail', 'stronger documentation', 'exhaustive certainty',
        'repeat information already supplied', 'availability of another question',
        'Do not manufacture a stronger claim', 'imperfect evidence does not',
        'Scoped descriptive answers', 'MOVE_ON', 'current_prompt is the immediate question',
        'Use relevant prior_turns', 'embedded commands', 'max-two-probe rule',
        'not private chain-of-thought', 'If challenge_warranted=true', 'If challenge_warranted=false'):
        assert clause in prompt
    assert 'Return exactly challenge_warranted, reason, next_prompt.' in prompt
    for cid in reviewed.REVIEWED_IDS:
        assert cid not in prompt
    assert 'Stage 1 determined' in prompt and 'fixed routing fact' in prompt


@pytest.mark.parametrize('cid,warranted,action', [
    ('unsupported_claim-003', True, 'CHALLENGE'), ('tradeoffs-001', True, 'CHALLENGE'),
    ('injection-004', True, 'CHALLENGE'), ('strong_complete-001', False, 'MOVE_ON'),
    ('multi_turn-001', False, 'MOVE_ON'), ('injection-002', False, 'MOVE_ON')])
def test_owner_reviewed_semantic_boundary_material(cid, warranted, action):
    # Checks approved annotation/configuration and mapping, not model predictions.
    cases = reviewed.select_development_cases([cid])
    expectations = live.reviewed_challenge_expectations(cases)
    assert expectations[cid] is warranted and cases[0].expected_action == action
    assert contract.map_stage2(contract.validate_stage2(
        second(warranted, 'Probe' if warranted else None))).action == action
    assert cases[0].answer not in provider.STAGE2_INSTRUCTIONS
    assert set(expectations) == live.STAGE2_IDS


@pytest.mark.parametrize('index,rule', [(0, 'overgeneralized'),
    (1, "decision's assumptions, consequences, costs, or alternatives"), (2, 'material evidential leap')])
def test_approved_contrastive_pairs_as_review_material_only(index, rule):
    artifact = json.loads(Path('evals/interviewer/two_stage_challenge_contrastive_review_development.json').read_text())
    assert artifact['annotation_status'] == 'project_owner_human_reviewed_approved'
    assert artifact['purpose'] == 'design_and_offline_test_material_only'
    assert artifact['main_dataset_member'] is False and len(artifact['pairs']) == 3
    pair = artifact['pairs'][index]
    assert pair['stage_1'] == dict(understandable_relevant=True, blocking_context_gap=False)
    assert pair['prior_turns'] == [] and pair['current_prompt']
    assert rule in provider.STAGE2_INSTRUCTIONS
    a, b = pair['examples']
    assert a['challenge_warranted'] is True and a['mapped_action'] == 'CHALLENGE'
    assert b['challenge_warranted'] is False and b['mapped_action'] == 'MOVE_ON'
    assert a['answer'] != b['answer']
    assert a['review_outcome'] == b['review_outcome'] == 'APPROVE'
    assert a['answer'] not in provider.STAGE2_INSTRUCTIONS
    assert b['answer'] not in provider.STAGE2_INSTRUCTIONS


# Captured before implementation: historical evaluators/material, production,
# frontend, dependencies, CI and earlier tests. No credential files are read.
HISTORICAL_HASHES = {'.github/workflows/ci.yml': '1a5e98883b7f9dab41e46e6414de65a50d8199b450cb352b1ad3f149fd07d0e0',
 '.gitignore': 'a90b04e7581389066e7dec54caedcdca482b3dd3eff3a4cfdd87785707fc9b14',
 'BUILD_LOG.md': 'ca3a43f87481fe32d2fe214bc0dd6cc1957db114ea2a7ee7ee862244c454b855',
 'README.md': '9c3ebecdeee29dc9167f2c4957f9fbd170a997af37aa3c85ec08d1e10133a38f',
 'backend/app/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
 'backend/app/audio.py': 'd7d1ef90dd893a80131e2bd9a9e2a5c2df72085f72ce140d63cc99e4e62aa3fa',
 'backend/app/interview_orchestration.py': '996f6a538c61bd2dcec544866727512b71a809f7f31a05b0b88751bf1ef99d71',
 'backend/app/main.py': '0310c563690a24669ca3bfcaf461617ff7bd20b2da3e5432d20b06e1d9fb77b4',
 'backend/app/nemotron.py': '3a5157713ad94650b83d86d0b18f37c48c57f93b04f7ebcfa72843d121d4c604',
 'backend/app/reasoning.py': '04df966e60ff9b815b2154e88de55ecda74c6967ac8df831374ec614371c14bc',
 'backend/app/session_routes.py': 'b3c2ae058e0ac4db9583923b9ab02e94041334ba61bc4205604001bebbf8e376',
 'backend/app/sessions.py': 'a833b66a1191171a5ae335e1227f7ca871e616eabc106949adee190286218739',
 'backend/app/transcription.py': '47137d467f96022195b750c3a01c2ce9c56a751ee9cec580835bef6aeea6e86e',
 'backend/requirements-dev.txt': 'ff321232f995e02be6a147527bc3bc5f988b2c29318a8b4ac9142c2ba2f4277f',
 'backend/requirements.txt': 'a3e4f481db61c43b2c52437bef65731fe5fe326e4f3ef2eda36206b3baa82012',
 'docs/.gitkeep': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
 'evals/interviewer/ASSESSMENT.md': '2b551bc8f49463e47509730bcaca64c38e383f970b610ff225db7ed3d819120c',
 'evals/interviewer/ASSESSMENT_ONLY.md': 'ac355db6fda39eee8f4ba644bb54d86e060f66c802daf07924bb026c79c7275f',
 'evals/interviewer/ASSESSMENT_V2.md': '3c2accddb5fc01a3d65b20bbd3639fc07ed405e925c93657a85123bb4045e077',
 'evals/interviewer/BLOCKING_CONTEXT.md': 'ea2557bae4c33c4d27020547eca34e29ec2302c061b946f0aa2e1330aed29370',
 'evals/interviewer/README.md': '5144fa63602e53f7f58fed5db88b797b9fb975d54c1e4ba6a91998041181c591',
 'evals/interviewer/TWO_STAGE.md': '2295000b46a4281d68df91b32dd91908caad3366d2ba7c418a69528674cb9cef',
 'evals/interviewer/assessment_independent.py': 'e0bed7d6e1d85ae462e6ce2fd622897481236366bc56700a7ac17ce1a3e96f4f',
 'evals/interviewer/assessment_live.py': '8673c8548cfe10a9b167ce7b8d2dd4327dbb1c9686bdbf7e88e3bdfe772bcf6e',
 'evals/interviewer/assessment_live_v2.py': '5075fb91e646e0179a761560200eaa5a698c532da29f159d670d2fe52166123c',
 'evals/interviewer/assessment_live_v3.py': '4af0dddf5b3f18863d188df2cccce3e9ef93e71cdaa2626bd5b48e845777daf2',
 'evals/interviewer/assessment_only.py': '2a938d23f44ad180fc8c5c09d47aadd99822916de1f17848510ed95668f9ef90',
 'evals/interviewer/assessment_only_live.py': 'a7b4717717784e1bd2e0889ff70105968e3c713741c40a8758bf1e48aba6538f',
 'evals/interviewer/assessment_prototype.py': '1c8aadb9915ff968086b8c107cd909b6d2a367cec64db2399ad8ca51e729cf9e',
 'evals/interviewer/blocking_context.py': '5a2ac6d06547cf70d85bb48c70c09126ff6d834c93d03ce1c6b2557f695a5129',
 'evals/interviewer/blocking_context_annotations_development.json': '4882d3d139889e21f7b80d99e723cc7a00ca70a70b7c662d844a54cb4d2fdac5',
 'evals/interviewer/blocking_context_boundary_cases_development.json': '70a9bf3e23991a02843f5075aaaf94a4332ac09daede1841e6790e11958c7956',
 'evals/interviewer/blocking_context_live.py': '45f184326f0fa2aad37f95869d73d8bfa89f47a8fe070d0da8c9389b40bdc942',
 'evals/interviewer/cases.jsonl': 'd496391a3f25ac8173e504794497c4aa4120526a0c7238f94164b3bd234dfc35',
 'evals/interviewer/evaluate.py': '3c6f4db841cd8df3e72b5211f430bb807a208c22221efbc14c0fba9557975309',
 'evals/interviewer/independent_annotations_development_draft.json': '24723e8e24ab6d025709b9e59a09e122c7fc390af20d8ff250487960f8ed350f',
 'evals/interviewer/two_stage_contract.py': 'ff60814b0d69d0410a9c3cc25595fa8971d146cbe8776f4d15b92fc81c2cab84',
 'evals/interviewer/two_stage_live.py': '6862bb3d31ccacf6c8275a0f51fc0ad029a3d76bba718047bcfdab69d19614ea',
 'evals/interviewer/two_stage_orchestration.py': 'a356ee9fbd54482868c023a1b59e461224b7207952db241098e6481d8de8d820',
 'evals/interviewer/two_stage_provider.py': '1fe7f8f69675139ab3f54961d60e8d10bd449a523a468dbe75139c238c87a55c',
 'frontend/.gitignore': '7fb01c05e47a92bd244458825b417d083d992a4ba5b705407e8971a8db1564ae',
 'frontend/.oxlintrc.json': 'b4d344818a1e1bf43997e4744a9153c8eef770c98b58229cd86363dabb85ded4',
 'frontend/e2e/interview-completion.pw.ts': 'c35bc3e10d054c8fad393685fc8a3a8dd3196be95d51ab1fd08de9f62eb6fbb7',
 'frontend/index.html': '5c2580760bd2fa2c2b3bc8dc771c935b7883dcf41082a7e402f9b7911ad26f6a',
 'frontend/package-lock.json': '42de3ffc11423daf5c4fb00e4abb6b775fbf8628aa2b762cf45e68e516896f8b',
 'frontend/package.json': '5f008c87322d5cf7277fcad288e4101ec9f8a3aced8c3de1b9f88e8c789490cd',
 'frontend/playwright.config.ts': 'e9318d21bd6a0b5a4bc1da172faa177e87798238dc4e5a95a235a4c1a5c4f202',
 'frontend/src/App.tsx': '1823a9481501c9e375737a5429c715b0f44db62b38ae0d4e6aa946930c1ce45f',
 'frontend/src/AudioAnswer.test.tsx': '13ebd059b7a72646d2af88c91acb6b009f72ec00726cf8e265e2e0d6e3f3c7ba',
 'frontend/src/AudioAnswer.tsx': '17c589326c19b683ef8b331ac49ad25cffa53fad0367362fd2eb10b55df5596b',
 'frontend/src/Interview.test.tsx': 'f3189eb1a60ed5dfd4e5baadd6353da73997e439d36bd320f23d4c6aa88fa069',
 'frontend/src/Interview.tsx': 'f37b3ddd573f18292f854656e33b0f80f4928746b6583bd2fdd2f0adad13ab9e',
 'frontend/src/index.css': '4edbd292b07635322018077433e0c956b491b5cb6131db77d58053cbb5091048',
 'frontend/src/interviewApi.ts': '062330dfcb56dd8024d483641621ebbf791e93d1c5dda38bc70b99cbc42c8329',
 'frontend/src/main.tsx': '6e9e5807fcbd48b75a96db5cbef36c996262196be42e6d4760dc86babbe61ad2',
 'frontend/src/useAudioRecorder.ts': '158989d05a680f2db1c8d0f87773a10d71d4c41a565048da6d30777b829d8d20',
 'frontend/tsconfig.app.json': '1b246d31e9698193a0876e8c2522c7956253374b6be831ba24f42f5480711749',
 'frontend/tsconfig.json': '770b4140bbb581e2dfd9ea9946ffc9c75a1d86ba7d2db5f77c83e37cbdf9d808',
 'frontend/tsconfig.node.json': '73b7ca4c9f059cc40843d80cb51386ab53910b442d9f53ed82f885da752296e6',
 'frontend/vite.config.ts': '9cc7fd97df98c7e4fbe5bded766b1bdf92dac431ec83c12ed190d7c7b242c406',
 'pytest.ini': 'b52fd5ec1eac97dce8cbbcd40f39ed3fa1b604a1a47ea225809c8ae1cea8a8e8',
 'tests/conftest.py': '6b8737a30d1fb1a2f53338b47fa26678271222fc946e442009092040e33bd914',
 'tests/e2e_app.py': 'bf35e40ca5c000092eb72ff145e5e154c4fd8b5e09ff37bab514f56016b3ecdb',
 'tests/test_assessment_live.py': '3276b6bf97d268066c71431f3c6274bb55274bac7dfed15ccd123f1ed60bac5b',
 'tests/test_assessment_only.py': '4243a4de35da347afd3ad1d830be587f91b991b406d41cb548318d232a1ad7c9',
 'tests/test_assessment_prototype.py': 'a3779e0a823365f1de7dee05886a613e92f580867af28b094401876e4e6d7c51',
 'tests/test_assessment_v2.py': 'c8a6f5c738e8720ad81a7c98f5a5c881b45f1604db323f2cc46064bbb83469e1',
 'tests/test_assessment_v3.py': 'fa12679da19332bd61e5c17f9a2e75b0e6de3ae354b942dea2a919021d8a5168',
 'tests/test_audio.py': 'e11d41e8219406d7412a6fa707fdb1613f93d4aec7edb617287f9f7b66ca0391',
 'tests/test_blocking_context.py': 'c8415441466ab09dcb0f3729a8708b8e6f229cc7ab4b02afb502752fb1c5fc3a',
 'tests/test_health.py': '6419aca7867aed2f75a82aa2c82d394ca7ed3b741c18266e0507fdd766dd2cbf',
 'tests/test_interview_reasoning.py': '2d310c2ab21440f2ac93a7f70d2a721b2ed057db73ce73bd24a62bc3191a0532',
 'tests/test_nemotron.py': 'bb8ca529d5d44e98d5f0e5a8ab1a7112360bbb89c1af6663216fffbb47f53523',
 'tests/test_reasoning.py': '73893cf3a8b854bb47d3808898987c4f0ebdfe4644a257fb8261a366da3b20db',
 'tests/test_reasoning_evaluation.py': '5727dd05038bf6653cc329aba1bfc8c595665f09f46a7e30d1a275fbbf53a5ce',
 'tests/test_representation_diagnostics.py': '42c4d5b86a61089ad743ba61c11ffbf143fd0740ee45cca0973b179da1bc2b08',
 'tests/test_sessions.py': '711ef6bcf46606924ec64263ff871172f53b3f4d1348c3bae3907cd3467332aa',
 'tests/test_transcription.py': '707b0a19cc3e55ac00f5678146895f15bb4e36839e247db372f0917e21c03ce4',
 'tests/test_two_stage.py': 'e521f7d186551e53927259b9aee0be8210a6094ff5f305ddf7f7f7c6787da019'}


@pytest.mark.parametrize('path,digest', sorted(HISTORICAL_HASHES.items()))
def test_historical_files_unchanged(path, digest):
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path


def test_production_has_no_candidate_imports():
    for path in Path('backend/app').glob('*.py'):
        assert 'two_stage_challenge' not in path.read_text()

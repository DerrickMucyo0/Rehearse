"""Offline model-only payload, isolation, operation and privacy regressions."""
import asyncio
import ast
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import sys
import textwrap
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.reasoning import (InvalidDecision, ReasoningContext, ReasoningFailed,
                           ReasoningTimeout, ReasoningUnavailable)
from app.sessions import AnswerRequest, InterviewSessionService, SessionConflict
from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer import two_stage_provider as super_provider
from evals.interviewer import two_stage_orchestration as operation
from evals.interviewer import two_stage_challenge_contract as contract
from evals.interviewer import two_stage_challenge_provider as challenge_provider
from evals.interviewer import two_stage_challenge_orchestration as challenge_operation
from evals.interviewer import two_stage_challenge_live as challenge_live
from evals.interviewer import two_stage_challenge_ultra_provider as provider
from evals.interviewer import two_stage_challenge_ultra_orchestration as orchestration
from evals.interviewer import two_stage_challenge_ultra_live as live


def first(u=True, gap=False, prompt=None):
    return dict(understandable_relevant=u, blocking_context_gap=gap,
                reason='PRIVATE_STAGE1_REASON', next_prompt=prompt)


def second(warranted=True, prompt='PRIVATE_STAGE2_PROMPT'):
    return dict(challenge_warranted=warranted, reason='PRIVATE_STAGE2_REASON', next_prompt=prompt)


def context():
    return ReasoningContext(question='PRIVATE_QUESTION', current_prompt='PRIVATE_CURRENT',
                            answer='PRIVATE_ANSWER')


def response(value, **message_updates):
    message = dict(role='assistant', content=json.dumps(value), reasoning_content='PRIVATE_HIDDEN_TRACE')
    message.update(message_updates)
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': message}]})


class Fake:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.contexts = value, error, []

    async def decide(self, ctx):
        self.contexts.append(ctx)
        if self.error:
            raise self.error
        return self.value


def test_prompt_parser_schema_semantics_and_annotations_reused_exactly():
    service = provider.UltraStageService()
    assert service.instructions.encode() == challenge_provider.STAGE2_INSTRUCTIONS.encode()
    assert service.parse_content is contract.parse_stage2
    assert provider.ChallengeStage2 is contract.ChallengeStage2
    assert contract.ChallengeStage2.model_json_schema()['required'] == [
        'challenge_warranted', 'reason', 'next_prompt']
    assert challenge_provider.CHALLENGE_DEFINITION in service.instructions
    assert live.reviewed_challenge_expectations is challenge_live.reviewed_challenge_expectations
    cases = reviewed.select_development_cases(list(challenge_live.STAGE2_IDS))
    assert live.reviewed_challenge_expectations(cases) == challenge_live.reviewed_challenge_expectations(cases)
    assert provider.EXPERIMENT_VERSION == 'interviewer-two-stage-challenge-ultra-v1'
    assert 'interviewer-two-stage-challenge-ultra-v1' not in service.instructions


def test_immutable_explicit_model_identity_and_no_global_replacement():
    service = provider.UltraStageService()
    assert service.model == 'nvidia/nemotron-3-ultra-550b-a55b'
    with pytest.raises(AttributeError):
        service.model = super_provider.MODEL
    assert super_provider.MODEL == provider.STAGE1_MODEL == 'nvidia/nemotron-3-super-120b-a12b'
    tree = ast.parse(Path(provider.__file__).read_text())
    assert not any(isinstance(node, ast.Global) for node in ast.walk(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not any(isinstance(t, ast.Attribute) and t.attr == 'MODEL' for t in targets)
            assert not any(isinstance(t, ast.Name) and t.id == 'MODEL' for t in targets)


def test_request_body_ast_identical_except_model_expression():
    def body(method):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        return ast.Module(body=tree.body[0].body, type_ignores=[])

    class Normalize(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'model':
                return ast.Name(id='MODEL', ctx=ast.Load())
            return self.generic_visit(node)

    assert ast.dump(Normalize().visit(body(provider.UltraStageService._request))) == ast.dump(
        body(super_provider.StageService._request))
    assert provider.UltraStageService.decide is challenge_provider.ChallengeStageService.decide


def test_payloads_only_differ_in_stage2_model(monkeypatch, caplog):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    sent = []

    def handler(req):
        value = json.loads(req.content)
        sent.append(value)
        assert str(req.url) == provider.ENDPOINT
        return response(first() if 'Stage 1.' in value['messages'][0]['content'] else second())

    transport = httpx.MockTransport(handler)
    one = super_provider.StageService('stage_1', transport)
    original = challenge_provider.ChallengeStageService(transport)
    ultra = provider.UltraStageService(transport)

    async def run():
        await one.decide(context())
        await original.decide(context())
        await ultra.decide(context())
        await original.decide(context())
        await one.decide(context())

    asyncio.run(run())
    assert [p['model'] for p in sent] == [provider.STAGE1_MODEL, provider.STAGE1_MODEL,
        provider.STAGE2_MODEL, provider.STAGE1_MODEL, provider.STAGE1_MODEL]
    assert {k: v for k, v in sent[1].items() if k != 'model'} == {
        k: v for k, v in sent[2].items() if k != 'model'}
    assert sent[1] == sent[3] and sent[0] == sent[4]
    for value in sent:
        assert set(value) == {'model', 'stream', 'messages', *asdict(super_provider.ASSESSMENT_CONFIG)}
        assert {key: value[key] for key in asdict(super_provider.ASSESSMENT_CONFIG)} == dict(
            temperature=1.0, top_p=.95, max_tokens=1024, reasoning_effort='high', reasoning_budget=256)
        assert value['stream'] is False
        assert value['messages'][1]['content'] == context().model_dump_json()
    assert 'PRIVATE' not in caplog.text


def test_concurrent_adapters_do_not_contaminate_models(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')

    async def run():
        sent, ready = [], asyncio.Event()

        async def handler(req):
            value = json.loads(req.content)
            sent.append(value)
            if len(sent) == 3:
                ready.set()
            await ready.wait()
            return response(first() if 'Stage 1.' in value['messages'][0]['content'] else second())

        transport = httpx.MockTransport(handler)
        services = [super_provider.StageService('stage_1', transport),
                    challenge_provider.ChallengeStageService(transport), provider.UltraStageService(transport)]
        results = await asyncio.gather(*(service.decide(context()) for service in services))
        assert results[0].understandable_relevant and results[1].challenge_warranted and results[2].challenge_warranted
        assert [value['model'] for value in sent] == [provider.STAGE1_MODEL, provider.STAGE1_MODEL, provider.STAGE2_MODEL]
    asyncio.run(run())


def test_concurrent_separate_reasoners_preserve_stage_models(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')

    async def run():
        sent = []

        async def handler(req):
            value = json.loads(req.content)
            sent.append(value)
            await asyncio.sleep(0)
            return response(first() if 'Stage 1.' in value['messages'][0]['content'] else second(False, None))

        transport = httpx.MockTransport(handler)
        old = challenge_operation.ChallengeReasoner(super_provider.StageService('stage_1', transport),
                                                   challenge_provider.ChallengeStageService(transport))
        new = orchestration.UltraReasoner(super_provider.StageService('stage_1', transport),
                                          provider.UltraStageService(transport))
        decisions = await asyncio.gather(old.decide(context()), new.decide(context()))
        assert [decision.action for decision in decisions] == ['MOVE_ON', 'MOVE_ON']
        stage1 = [p for p in sent if 'Stage 1.' in p['messages'][0]['content']]
        stage2 = [p for p in sent if 'Stage 2.' in p['messages'][0]['content']]
        assert len(stage1) == len(stage2) == 2
        assert all(p['model'] == provider.STAGE1_MODEL for p in stage1)
        assert sorted(p['model'] for p in stage2) == sorted([provider.STAGE1_MODEL, provider.STAGE2_MODEL])
    asyncio.run(run())


def test_operation_and_session_harness_identity():
    assert orchestration.UltraReasoner.decide is challenge_operation.ChallengeReasoner.decide
    assert orchestration.UltraReasoner._stage is operation.TwoStageReasoner._stage
    assert orchestration.submit_experimental is operation.submit_experimental
    reasoner = orchestration.UltraReasoner()
    assert type(reasoner.stage1) is super_provider.StageService
    assert reasoner.stage1.instructions == super_provider.STAGE1_INSTRUCTIONS
    assert type(reasoner.stage2.provider) is provider.UltraStageService
    assert operation.COMBINED_DEADLINE_SECONDS == provider.PROVIDER_TIMEOUT_SECONDS == 30


@pytest.mark.parametrize('u,gap,prompt,action', [(False, None, 'Meaning?', 'CLARIFY'),
                                              (True, True, 'Result?', 'FOLLOW_UP')])
def test_terminal_stage1_never_sends_ultra(monkeypatch, u, gap, prompt, action):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        assert sent[-1]['model'] == provider.STAGE1_MODEL
        return response(first(u, gap, prompt))

    transport = httpx.MockTransport(handler)
    reasoner = orchestration.UltraReasoner(super_provider.StageService('stage_1', transport),
                                          provider.UltraStageService(transport))
    assert asyncio.run(reasoner.decide(context())).action == action
    assert len(sent) == 1 and not reasoner.last_trace.stage_2.attempted


@pytest.mark.parametrize('signal,action', [(True, 'CHALLENGE'), (False, 'MOVE_ON')])
def test_continue_calls_ultra_once_and_safe_report(monkeypatch, caplog, capsys, signal, action):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return response(first() if len(sent) == 1 else second(signal, 'PRIVATE_PROBE' if signal else None))

    transport = httpx.MockTransport(handler)
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),
        orchestration.UltraReasoner(super_provider.StageService('stage_1', transport), provider.UltraStageService(transport))))
    assert [p['model'] for p in sent] == [provider.STAGE1_MODEL, provider.STAGE2_MODEL]
    assert sent[0]['messages'][1] == sent[1]['messages'][1]
    assert 'PRIVATE_STAGE1_REASON' not in json.dumps(sent[1])
    assert report['outcomes'][0]['predicted'] == action and report['stage_call_attempts'] == 2
    assert report['model'] == report['stage_2_model'] == provider.STAGE2_MODEL
    assert report['stage_1_model'] == provider.STAGE1_MODEL
    assert report['experiment_version'] == provider.EXPERIMENT_VERSION
    assert report['prompt_version'] == challenge_provider.PROMPT_VERSION
    assert report['stage_2_annotations_sha256'] == challenge_live.ANNOTATIONS_SHA256
    assert 'PRIVATE' not in json.dumps(report) + caplog.text + capsys.readouterr().out
    assert 'reason' not in report['outcomes'][0] and 'next_prompt' not in report['outcomes'][0]


@pytest.mark.parametrize('mode,error,invalid,json_reason,schema,kind,status', [
    ('syntax', 'invalid_output', 'json_syntax', 'other_json_syntax', None, None, None),
    ('duplicate', 'invalid_output', 'duplicate_json_key', None, None, None, None),
    ('constant', 'invalid_output', 'non_json_constant', None, None, None, None),
    ('prefix', 'invalid_output', 'json_syntax', 'trailing_data', None, None, None),
    ('schema', 'invalid_output', 'schema_validation', None, 'missing_required_field', None, None),
    ('consistency', 'invalid_output', 'schema_validation', None, 'assessment_prompt_inconsistency', None, None),
    ('envelope', 'invalid_output', 'response_envelope', None, None, None, None),
    ('finish', 'invalid_output', 'finish_reason', None, None, None, None),
    ('tool', 'invalid_output', 'tool_or_function_call', None, None, None, None),
    ('refusal', 'invalid_output', 'refusal', None, None, None, None),
    ('content_type', 'invalid_output', 'content_type', None, None, None, None),
    ('content_size', 'invalid_output', 'content_size', None, None, None, None),
    ('response_size', 'invalid_output', 'response_size', None, None, None, None),
    ('401', 'provider_error', None, None, None, 'http_status', 401),
    ('403', 'provider_error', None, None, None, 'http_status', 403),
    ('429', 'provider_error', None, None, None, 'http_status', 429),
    ('500', 'provider_error', None, None, None, 'http_status', 500),
    ('transport', 'provider_error', None, None, None, 'transport', None),
    ('timeout', 'timeout', None, None, None, None, None),
    ('adapter', 'provider_error', None, None, None, 'adapter_error', None)])
def test_ultra_failures_preserve_diagnostics_and_never_recover_hidden_content(
    monkeypatch, caplog, capsys, mode, error, invalid, json_reason, schema, kind, status):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return response(first())
        assert calls[-1]['model'] == provider.STAGE2_MODEL
        if mode.isdigit():
            return httpx.Response(int(mode), text='PRIVATE_BODY')
        if mode == 'transport':
            raise httpx.ConnectError('PRIVATE_EXCEPTION')
        if mode == 'timeout':
            raise httpx.ReadTimeout('PRIVATE_EXCEPTION')
        if mode == 'adapter':
            raise RuntimeError('PRIVATE_EXCEPTION')
        if mode == 'response_size':
            return httpx.Response(200, content=b'x'*(provider.MAX_RESPONSE_BYTES+1))
        content = json.dumps(second())
        if mode == 'syntax': content = 'PRIVATE_BROKEN'
        if mode == 'duplicate': content = '{"x":1,"x":2}'
        if mode == 'constant': content = 'NaN'
        if mode == 'prefix': content += ' PRIVATE_SUFFIX'
        if mode == 'schema': content = '{}'
        if mode == 'consistency': content = json.dumps(second(True, None))
        if mode == 'content_type': content = None
        if mode == 'content_size': content = ' '*8193
        hidden = second()
        hidden['reason'] = 'UNIQUE_HIDDEN_TRACE'
        message = dict(role='assistant', content=content, reasoning_content=json.dumps(hidden))
        if mode == 'envelope': message = []
        if mode == 'tool': message['tool_calls'] = [{'id': 'PRIVATE_TOOL'}]
        if mode == 'refusal': message['refusal'] = 'PRIVATE_REFUSAL'
        return httpx.Response(200, json={'choices': [{
            'finish_reason': 'length' if mode == 'finish' else 'stop', 'message': message}]})

    transport = httpx.MockTransport(handler)
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['unsupported_claim-003']),
        orchestration.UltraReasoner(super_provider.StageService('stage_1', transport), provider.UltraStageService(transport))))
    row = report['outcomes'][0]
    assert len(calls) == 2 and row['failure_stage'] == 'stage_2' and row['predicted'] is None
    assert (row['error'], row['invalid_reason'], row['json_reason'], row['schema_reason'],
            row['failure_kind'], row['http_status']) == (error, invalid, json_reason, schema, kind, status)
    assert not row['mapped_action_correct'] and not row['stages']['stage_2']['valid']
    assert row['stages']['stage_2']['challenge_warranted'] is None
    assert 'PRIVATE' not in json.dumps(report) + caplog.text + capsys.readouterr().out
    assert 'UNIQUE_HIDDEN_TRACE' not in json.dumps(report) + caplog.text


def test_unavailable_and_request_size_fail_before_transport(monkeypatch):
    def forbidden(req):
        pytest.fail('Transport must not be called')
    service = provider.UltraStageService(httpx.MockTransport(forbidden))
    with pytest.raises(ReasoningUnavailable):
        asyncio.run(service.decide(context()))
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    monkeypatch.setattr(provider, 'MAX_REQUEST_BYTES', 1)
    with pytest.raises(ReasoningFailed) as caught:
        asyncio.run(service.decide(context()))
    assert caught.value.failure.failure_kind == 'request_size'


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value


def test_combined_deadline_remaining_budget(monkeypatch):
    clock, budgets = Clock(), []
    async def wait(awaitable, timeout):
        budgets.append(timeout)
        result = await awaitable
        clock.value += 7 if len(budgets) == 1 else 4
        return result
    monkeypatch.setattr(operation, 'wait_for', wait)
    reasoner = orchestration.UltraReasoner(Fake(first()), Fake(second()), clock=clock)
    assert asyncio.run(reasoner.decide(context())).action == 'CHALLENGE'
    assert budgets == [30, 23] and reasoner.last_trace.latency_ms == 11000


def test_deadline_exhaustion_prevents_ultra_call(monkeypatch):
    clock, original = Clock(), operation.route_stage1
    def route(value):
        result = original(value)
        clock.value = 31
        return result
    monkeypatch.setattr(operation, 'route_stage1', route)
    two = Fake(second())
    reasoner = orchestration.UltraReasoner(Fake(first()), two, clock=clock)
    with pytest.raises(ReasoningTimeout): asyncio.run(reasoner.decide(context()))
    assert not two.contexts and not reasoner.last_trace.stage_2.attempted
    assert reasoner.last_trace.failure_stage == 'stage_2'


def test_shared_deadline_cancels_ultra_without_retry(monkeypatch):
    monkeypatch.setattr(operation, 'COMBINED_DEADLINE_SECONDS', .025)
    class Waiting(Fake):
        cancelled = False
        async def decide(self, ctx):
            self.contexts.append(ctx)
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
    two = Waiting()
    reasoner = orchestration.UltraReasoner(Fake(first()), two)
    with pytest.raises(ReasoningTimeout): asyncio.run(reasoner.decide(context()))
    assert two.cancelled and len(two.contexts) == 1
    assert reasoner.last_trace.failure_stage == 'stage_2'


def test_actual_wrong_gate_still_invokes_ultra_without_annotation_oracle():
    one, two = Fake(first()), Fake(second())
    report = asyncio.run(live.experiment_report(reviewed.select_development_cases(['blocking_context-003']),
                                               orchestration.UltraReasoner(one, two)))
    row = report['outcomes'][0]
    assert len(two.contexts) == 1 and row['unexpected_stage_2']
    assert row['expected'] == 'FOLLOW_UP' and row['predicted'] == 'CHALLENGE'
    assert row['stages']['stage_2']['assessments']['challenge_warranted'] == {
        'expected': None, 'predicted': True, 'applicable': False, 'correct': None}


def request(session, text='Draft', submission_id=None):
    return AnswerRequest(question_index=session.current_question_index, turn_revision=session.turn_revision,
                         submission_id=submission_id or uuid4(), answer=text)


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('failure', [InvalidDecision(invalid_reason='json_syntax'),
                                   ReasoningTimeout(), ReasoningFailed()])
def test_failure_atomicity_no_transition_or_consumption(stage, failure):
    sessions = InterviewSessionService()
    session = sessions.start()
    one = Fake(first(), failure if stage == 1 else None)
    two = Fake(second(), failure if stage == 2 else None)
    reasoner = orchestration.UltraReasoner(one, two)
    with pytest.raises(type(failure)):
        asyncio.run(orchestration.submit_experimental(sessions, session.id, request(session), reasoner))
    assert sessions.get(session.id) == session and session.id not in sessions._pending
    assert len(one.contexts) == 1 and len(two.contexts) == (stage == 2)


@pytest.mark.parametrize('stage', [1, 2])
def test_stale_turn_discard(stage):
    sessions = InterviewSessionService()
    session = sessions.start()
    class Stale(Fake):
        async def decide(self, ctx):
            result = await super().decide(ctx)
            with sessions._lock: sessions._sessions[session.id].turn_revision += 1
            return result
    one = Stale(first()) if stage == 1 else Fake(first())
    two = Stale(second()) if stage == 2 else Fake(second())
    reasoner = orchestration.UltraReasoner(one, two)
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions, session.id, request(session), reasoner))
    current = sessions.get(session.id)
    assert current.answers == [] and current.turns == [] and current.probe_count == 0
    assert len(two.contexts) == (stage == 2)


def test_replay_and_probe_budget_zero_extra_calls():
    sessions = InterviewSessionService()
    current = sessions.start()
    one, two = Fake(first()), Fake(second())
    reasoner = orchestration.UltraReasoner(one, two)
    answer = request(current)
    current = asyncio.run(orchestration.submit_experimental(sessions, current.id, answer, reasoner))
    assert asyncio.run(orchestration.submit_experimental(sessions, current.id, answer, reasoner)) == current
    assert len(one.contexts) == len(two.contexts) == 1
    with pytest.raises(SessionConflict):
        asyncio.run(orchestration.submit_experimental(sessions, current.id,
            answer.model_copy(update={'answer': 'Changed'}), reasoner))
    for _ in range(2):
        current = asyncio.run(orchestration.submit_experimental(sessions, current.id, request(current), reasoner))
    assert len(one.contexts) == len(two.contexts) == 2
    assert current.turns[-1].transition_source == 'probe_limit' and current.turns[-1].action is None


@pytest.mark.parametrize('stage', [1, 2])
def test_no_lock_spans_network_wait(stage):
    async def run():
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
        reasoner = orchestration.UltraReasoner(one, two)
        task = asyncio.create_task(orchestration.submit_experimental(sessions, session.id, request(session), reasoner))
        await entered.wait()
        assert sessions._lock.acquire(blocking=False)
        sessions._lock.release()
        assert sessions.get(session.id) == session
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert sessions.get(session.id) == session and session.id not in sessions._pending
    asyncio.run(run())


@pytest.mark.parametrize('argv', [
    ['ultra', '--case-id', 'unsupported_claim-003'], ['ultra', '--live'],
    ['ultra', '--live', '--case-id', 'unknown-001'],
    ['ultra', '--live', '--case-id', 'unsupported_claim-003', '--split', 'held_out'],
    ['ultra', '--live', '--case-id', 'unsupported_claim-003', 'unsupported_claim-003']])
def test_cli_guards_before_provider_construction(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(live, 'UltraReasoner', lambda: pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit): live.main()


# Frozen before implementation, including prior experiments, production, CI,
# dependencies, frontend and reviewed artifacts. Credential files are excluded.
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
 'evals/interviewer/TWO_STAGE_CHALLENGE.md': '47a554fb4bf5baf7f04db8e1ecb445e46bb04799d99bfef181b819cb32b21dff',
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
 'evals/interviewer/two_stage_challenge_annotations_development.json': '4f0348c3cf0b422745668f15a28fd3e88943439c2b29452d020e5faabd1867f3',
 'evals/interviewer/two_stage_challenge_contract.py': '6a8e1f9a3d967d35b9f67a85f0d256c894cee9bd9c7c40ee07eddc76c268caa0',
 'evals/interviewer/two_stage_challenge_contrastive_review_development.json': 'ad1a78e1b9e35f36350d9231cddce025f9c16176f95a6d18a8da62cdb63c519c',
 'evals/interviewer/two_stage_challenge_live.py': '6c9fcc7b4b7b0d37f28095570e4994736c8976c41d5821394f6987c034898602',
 'evals/interviewer/two_stage_challenge_orchestration.py': 'ea0d863cf7d725c7e4824d82e1f646910a7c56921ff315418003746ece15fb06',
 'evals/interviewer/two_stage_challenge_provider.py': 'b73f5f02649bc23a9be7a01f0d011bb57eb74a9ee6f623539636c490253ce4bf',
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
 'tests/test_two_stage.py': 'e521f7d186551e53927259b9aee0be8210a6094ff5f305ddf7f7f7c6787da019',
 'tests/test_two_stage_challenge.py': 'bf00eeefb824584ba10fa85918b3dc72c99dad02c7932b5889ba58852872fd09'}


@pytest.mark.parametrize('path,digest', sorted(HISTORICAL_HASHES.items()))
def test_historical_files_unchanged(path, digest):
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path


def test_no_production_selector_or_import():
    for path in Path('backend/app').glob('*.py'):
        assert 'two_stage_challenge_ultra' not in path.read_text()

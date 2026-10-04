"""Offline matched request/contract/report tests, never model intelligence."""
import asyncio
import ast
from dataclasses import asdict, FrozenInstanceError
import hashlib
import inspect
import itertools
import json
from pathlib import Path
import sys
import textwrap

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.reasoning import (InvalidDecision, ProviderFailure, ReasoningContext,
    ReasoningFailed, ReasoningTimeout, ReasoningUnavailable)
from evals.interviewer import stage2_model_match as live
from evals.interviewer import stage2_model_match_provider as provider
from evals.interviewer import two_stage_challenge_contract as contract
from evals.interviewer import two_stage_challenge_provider as historical
from evals.interviewer import two_stage_challenge_live as reviewed_challenge
from evals.interviewer import two_stage_provider as historical_provider


def value(signal=True, **updates):
    result = {'challenge_warranted': signal, 'reason': 'PRIVATE_REASON',
              'next_prompt': 'PRIVATE_NEXT_PROMPT' if signal else None}
    result.update(updates)
    return result


def context():
    return ReasoningContext(question='PRIVATE_QUESTION', current_prompt='PRIVATE_CURRENT',
                            answer='PRIVATE_ANSWER')


def response(content=None, **updates):
    message = {'role': 'assistant', 'content': json.dumps(value()) if content is None else content,
               'reasoning_content': 'PRIVATE_HIDDEN_TRACE', 'reasoning': 'PRIVATE_HIDDEN_TRACE'}
    message.update(updates)
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': message}]})


def services(handler):
    transport = httpx.MockTransport(handler)
    return {model: provider.MatchedStage2Service(model, transport) for model in provider.MODELS}


def run_report(handler):
    return asyncio.run(live.experiment_report(live.select_cases(), services(handler)))


def test_frozen_version_prompt_schema_and_review_identity():
    assert provider.EXPERIMENT_VERSION == 'interviewer-stage2-model-match-v1'
    assert live.PROMPT_VERSION == historical.PROMPT_VERSION == 'interviewer-two-stage-challenge-v1'
    assert provider.ChallengeStage2 is contract.ChallengeStage2
    assert live.validate_stage2 is contract.validate_stage2
    assert live.map_stage2 is contract.map_stage2
    assert live.reviewed_challenge_expectations is reviewed_challenge.reviewed_challenge_expectations
    assert live.ANNOTATIONS_SHA256 == reviewed_challenge.ANNOTATIONS_SHA256
    cases = live.select_cases()
    expected = live.validate_cases(cases)
    assert tuple(case.id for case in cases) == live.CASE_IDS
    assert all(case.split == 'development' for case in cases)
    assert [expected[case.id] for case in cases] == [True, True, True, False, False, False]
    original = live.reviewed.select_development_cases(live.CASE_IDS)
    assert [case.context().model_dump_json() for case in cases] == [case.context().model_dump_json() for case in original]
    for model in provider.MODELS:
        service = provider.MatchedStage2Service(model)
        assert service.instructions is historical.STAGE2_INSTRUCTIONS
        assert service.parse_content is contract.parse_stage2
        assert provider.EXPERIMENT_VERSION not in service.instructions
        assert service.decide.__func__ is historical.ChallengeStageService.decide


@pytest.mark.parametrize('model', provider.MODELS)
@pytest.mark.parametrize('attribute,replacement', [('model', 'other'), ('config', None),
    ('instructions', 'other'), ('_transport', None)])
def test_adapter_identity_and_configuration_immutable(model, attribute, replacement):
    service = provider.MatchedStage2Service(model)
    with pytest.raises(FrozenInstanceError):
        setattr(service, attribute, replacement)
    assert service.model == model
    assert asdict(service.config) == dict(temperature=1.0, top_p=.95, max_tokens=1024, reasoning_effort='high')
    assert not hasattr(service.config, 'reasoning_budget')
    with pytest.raises(FrozenInstanceError):
        service.config.max_tokens = 1


@pytest.mark.parametrize('model', ['other', '', None, provider.SUPER_MODEL+' ', 'nvidia/'+provider.ULTRA_MODEL])
def test_unknown_models_rejected(model):
    with pytest.raises(ValueError):
        provider.MatchedStage2Service(model)


def test_payloads_differ_only_in_model_and_context_identical(monkeypatch, caplog):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        assert str(request.url) == provider.ENDPOINT
        assert request.method == 'POST' and not request.url.query
        return response()
    adapters = services(handler)
    for model in provider.MODELS:
        asyncio.run(adapters[model].decide(context()))
    assert [item['model'] for item in sent] == list(provider.MODELS)
    assert {k: v for k, v in sent[0].items() if k != 'model'} == {k: v for k, v in sent[1].items() if k != 'model'}
    for item in sent:
        assert set(item) == {'model', 'stream', 'messages', 'temperature', 'top_p', 'max_tokens', 'reasoning_effort'}
        assert 'reasoning_budget' not in item and item['stream'] is False
        assert {key: item[key] for key in asdict(provider.CONFIG)} == asdict(provider.CONFIG)
        assert item['messages'] == [{'role': 'system', 'content': historical.STAGE2_INSTRUCTIONS},
                                   {'role': 'user', 'content': context().model_dump_json()}]
    assert provider.RETRIES == 0 and 'PRIVATE' not in caplog.text


def test_historical_request_path_ast_unchanged_except_model_and_omitted_budget():
    def body(method):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        return ast.Module(body=tree.body[0].body, type_ignores=[])
    class Normalize(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'model':
                return ast.Name(id='MODEL', ctx=ast.Load())
            return self.generic_visit(node)
        def visit_Dict(self, node):
            pairs = [(k, v) for k, v in zip(node.keys, node.values)
                     if not (isinstance(k, ast.Constant) and k.value == 'reasoning_budget')]
            node.keys, node.values = [p[0] for p in pairs], [p[1] for p in pairs]
            return self.generic_visit(node)
    assert ast.dump(Normalize().visit(body(provider.MatchedStage2Service._request))) == ast.dump(
        Normalize().visit(body(historical_provider.StageService._request)))


def test_concurrent_adapters_isolate_model_outputs(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    async def run():
        sent, ready = [], asyncio.Event()
        async def handler(request):
            item = json.loads(request.content)
            sent.append(item)
            if len(sent) == 2:
                ready.set()
            await ready.wait()
            return response(json.dumps(value(item['model'] == provider.SUPER_MODEL)))
        adapters = services(handler)
        one, two = await asyncio.gather(*(adapters[m].decide(context()) for m in provider.MODELS))
        assert one.challenge_warranted is True and two.challenge_warranted is False
        assert [p['model'] for p in sent] == list(provider.MODELS)
        assert historical_provider.MODEL == provider.SUPER_MODEL
    asyncio.run(run())


def test_order_exactly_twelve_and_no_stage1_inference(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    def forbidden(*args, **kwargs):
        raise AssertionError('Stage 1 must not be invoked')
    monkeypatch.setattr(historical_provider.StageService, '__init__', forbidden)
    sent = []
    def handler(request):
        item = json.loads(request.content)
        sent.append(item)
        return response()
    report = run_report(handler)
    assert report['attempted'] == len(sent) == 12
    assert [(row['case_id'], row['model_id']) for row in report['attempts']] == list(live.CALL_ORDER)
    assert [p['model'] for p in sent] == [model for _, model in live.CALL_ORDER]
    assert len(set(live.CALL_ORDER)) == 12
    assert live.CALL_ORDER == tuple((live.CASE_IDS[i], model) for i, models in enumerate([
        provider.MODELS, provider.MODELS[::-1], provider.MODELS, provider.MODELS[::-1],
        provider.MODELS, provider.MODELS[::-1]]) for model in models)
    cases = {case.id: case for case in live.select_cases()}
    for item, (cid, _) in zip(sent, live.CALL_ORDER):
        assert item['messages'][1]['content'] == cases[cid].context().model_dump_json()
    assert report['stage_1_invoked'] is False and 'Owner-reviewed' in report['routing_premise']
    assert report['combined_interview_deadline_tested'] is False
    assert report['timeout_seconds_per_stage2_call'] == 30 and report['retries'] == 0
    assert 'stages' not in report and 'stage_1_result' not in json.dumps(report)


def test_matched_paired_summary_and_failure_denominators(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    def handler(request):
        model = json.loads(request.content)['model']
        return response(json.dumps(value(model == provider.SUPER_MODEL)))
    report = run_report(handler)
    summary = report['paired_summary']
    assert summary['valid_pairs'] == 6
    assert summary['ultra_recoveries'] == summary['ultra_regressions'] == 3
    assert summary['unchanged_correct'] == summary['unchanged_wrong'] == 0
    def failed(request):
        if json.loads(request.content)['model'] == provider.SUPER_MODEL:
            return httpx.Response(503, text='PRIVATE_BODY')
        return response()
    report = run_report(failed)
    assert report['paired_summary']['valid_pairs'] == 0
    assert report['paired_summary']['ultra_recoveries'] == 0
    assert all(pair['comparison'] == 'Super failure' for pair in report['pairs'])
    assert report['model_metrics'][provider.SUPER_MODEL]['attempted'] == 6
    assert report['model_metrics'][provider.SUPER_MODEL]['provider_errors'] == 6
    assert report['model_metrics'][provider.SUPER_MODEL]['correct'] == 0


def test_timeout_enforced_per_call_without_retry(monkeypatch):
    calls = []
    class Slow:
        def __init__(self, model): self.model = model
        async def decide(self, ctx):
            calls.append(self.model)
            await asyncio.sleep(1)
            return value()
    monkeypatch.setattr(live, 'PROVIDER_TIMEOUT_SECONDS', .001)
    report = asyncio.run(live.experiment_report(live.select_cases(),
        {model: Slow(model) for model in provider.MODELS}))
    assert len(calls) == 12
    assert all(row['error'] == 'timeout' and row['predicted_challenge_warranted'] is None
               and row['invalid_reason'] is None for row in report['attempts'])
    assert all(summary['timeouts'] == 6 for summary in report['model_metrics'].values())
    assert inspect.signature(httpx.AsyncHTTPTransport).parameters['retries'].default == provider.RETRIES == 0


def test_forged_assessment_revalidated_without_semantic_inference():
    class Forged:
        model = provider.SUPER_MODEL
        async def decide(self, ctx):
            return contract.ChallengeStage2.model_construct(**value(next_prompt=None))
    case = live.select_cases()[0]
    row = asyncio.run(live._attempt(case, Forged(), True))
    assert row['error'] == 'invalid_output' and row['schema_reason'] == 'assessment_prompt_inconsistency'
    assert row['predicted_challenge_warranted'] is None and row['mapped_action'] is None


@pytest.mark.parametrize('signal', [True, False])
def test_mapping_scoring_never_uses_reason_or_prompt(monkeypatch, signal):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    misleading = 'CHALLENGE MOVE_ON FOLLOW_UP CLARIFY'
    report = run_report(lambda _: response(json.dumps(value(signal, reason=misleading,
        next_prompt=misleading if signal else None))))
    for row in report['attempts']:
        assert row['mapped_action'] == ('CHALLENGE' if signal else 'MOVE_ON')
        assert row['correct'] == (row['expected_challenge_warranted'] is signal)
    for summary in report['model_metrics'].values():
        assert summary['valid'] == 6 and summary['correct'] == 3
        assert summary['false_positive_challenges'] == (3 if signal else 0)
        assert summary['missed_challenges'] == (0 if signal else 3)


@pytest.mark.parametrize('content,reason,json_reason,schema_reason', [
    ('{', 'json_syntax', 'json_error_at_end', None),
    ('prefix {}', 'json_syntax', 'other_json_syntax', None),
    ('{} trailing', 'json_syntax', 'trailing_data', None),
    ('{"a":1,"a":2}', 'duplicate_json_key', None, None),
    ('{"a":NaN}', 'non_json_constant', None, None),
    ('{}', 'schema_validation', None, 'missing_required_field'),
    (json.dumps(value(challenge_warranted='true')), 'schema_validation', None, 'strict_type_violation'),
    (json.dumps(value(next_prompt=None)), 'schema_validation', None, 'assessment_prompt_inconsistency'),
    (json.dumps(value(extra='PRIVATE')), 'schema_validation', None, 'forbidden_extra_field'),
    (json.dumps(value(reason='')), 'schema_validation', None, 'text_bound_violation'),
    (json.dumps(value(reason='x'*301)), 'schema_validation', None, 'text_bound_violation'),
    (json.dumps(value(next_prompt='x'*501)), 'schema_validation', None, 'text_bound_violation'),
    (123, 'content_type', None, None), ('x'*8193, 'content_size', None, None)])
def test_invalid_content_preserves_diagnostics_and_never_retries(monkeypatch, content, reason, json_reason, schema_reason):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []
    def handler(request):
        calls.append(request)
        return response(content)
    report = run_report(handler)
    assert len(calls) == 12
    for row in report['attempts']:
        assert not row['valid'] and row['predicted_challenge_warranted'] is None
        assert not row['correct'] and row['mapped_action'] is None
        assert (row['error'], row['invalid_reason'], row['json_reason'], row['schema_reason']) == (
            'invalid_output', reason, json_reason, schema_reason)
    assert report['paired_summary']['valid_pairs'] == 0
    assert report['paired_summary']['ultra_recoveries'] == 0
    assert all(pair['comparison'] == 'both failure' for pair in report['pairs'])


@pytest.mark.parametrize('update,invalid_reason', [({'message': []}, 'response_envelope'),
    ({'finish_reason': 'length'}, 'finish_reason'), ({'tool_calls': [{}]}, 'tool_or_function_call'),
    ({'function_call': {'name': 'PRIVATE'}}, 'tool_or_function_call'), ({'refusal': 'PRIVATE'}, 'refusal')])
def test_envelope_diagnostics(monkeypatch, update, invalid_reason):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    def handler(_):
        if 'message' in update or 'finish_reason' in update:
            choice = {'message': {'role': 'assistant', 'content': json.dumps(value())}, 'finish_reason': 'stop', **update}
            return httpx.Response(200, json={'choices': [choice]})
        return response(**update)
    report = run_report(handler)
    assert all(row['invalid_reason'] == invalid_reason for row in report['attempts'])


@pytest.mark.parametrize('status', [400, 401, 403, 429, 500, 503])
def test_http_failures_safe_no_retry(monkeypatch, status, caplog):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='PRIVATE_PROVIDER_BODY_PRIVATE_KEY', headers={'x-secret': 'PRIVATE_HEADER'})
    report = run_report(handler)
    assert len(calls) == 12
    for row in report['attempts']:
        assert row['error'] == 'provider_error' and row['failure_kind'] == 'http_status'
        assert row['http_status'] == status and row['invalid_reason'] is None
    assert 'PRIVATE' not in json.dumps(report) + caplog.text


@pytest.mark.parametrize('exception,error,kind', [(httpx.ConnectError('PRIVATE_EXCEPTION'), 'provider_error', 'transport'),
    (httpx.ReadTimeout('PRIVATE_EXCEPTION'), 'timeout', None),
    (RuntimeError('PRIVATE_EXCEPTION'), 'provider_error', 'adapter_error')])
def test_transport_timeout_adapter_sanitization(monkeypatch, exception, error, kind, caplog):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    calls = []
    def handler(request):
        calls.append(request)
        raise exception
    report = run_report(handler)
    assert len(calls) == 12
    assert all((row['error'], row['failure_kind'], row['http_status']) == (error, kind, None)
               for row in report['attempts'])
    assert 'PRIVATE' not in json.dumps(report) + caplog.text


def test_unavailable_request_size_and_response_size(monkeypatch):
    calls = []
    adapters = services(lambda request: calls.append(request) or response())
    report = asyncio.run(live.experiment_report(live.select_cases(), adapters))
    assert not calls and all(row['failure_kind'] == 'unavailable' for row in report['attempts'])
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    monkeypatch.setattr(provider, 'MAX_REQUEST_BYTES', 1)
    report = asyncio.run(live.experiment_report(live.select_cases(), adapters))
    assert not calls and all(row['failure_kind'] == 'request_size' for row in report['attempts'])
    monkeypatch.setattr(provider, 'MAX_REQUEST_BYTES', 150000)
    monkeypatch.setattr(provider, 'MAX_RESPONSE_BYTES', 1)
    report = asyncio.run(live.experiment_report(live.select_cases(), adapters))
    assert len(calls) == 12 and all(row['invalid_reason'] == 'response_size' for row in report['attempts'])


def test_report_privacy_allowlist_and_hidden_trace_exclusion(monkeypatch, caplog, capsys):
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    report = run_report(lambda _: response())
    serialized = json.dumps(report)
    assert 'PRIVATE' not in serialized + caplog.text + capsys.readouterr().out
    assert historical.STAGE2_INSTRUCTIONS not in serialized
    for case in live.select_cases():
        assert case.answer not in serialized and case.current_prompt not in serialized
        assert case.question not in serialized and case.context().model_dump_json() not in serialized
        for turn in case.prior_turns:
            assert turn.prompt not in serialized and turn.answer not in serialized
    allowed = {'case_id', 'model_id', 'valid', 'expected_challenge_warranted', 'predicted_challenge_warranted',
        'correct', 'expected_action', 'mapped_action', 'latency_ms', 'error', 'invalid_reason', 'json_reason',
        'schema_reason', 'failure_kind', 'http_status', 'attempt_index'}
    assert all(set(row) == allowed for row in report['attempts'])


@pytest.mark.parametrize('super_valid,ultra_valid,super_correct,ultra_correct,expected', [
    (True, True, True, True, 'both correct'), (True, True, True, False, 'Super only correct'),
    (True, True, False, True, 'Ultra only correct'), (True, True, False, False, 'both wrong'),
    (False, True, False, True, 'Super failure'), (True, False, True, False, 'Ultra failure'),
    (False, False, False, False, 'both failure')])
def test_paired_classification(super_valid, ultra_valid, super_correct, ultra_correct, expected):
    assert live.paired_classification({'valid': super_valid, 'correct': super_correct},
                                    {'valid': ultra_valid, 'correct': ultra_correct}) == expected


@pytest.mark.parametrize('mutation', ['split', 'answer', 'label', 'order', 'duplicate', 'subset', 'extra'])
def test_changed_or_unreviewed_cases_rejected_before_provider_construction(monkeypatch, mutation):
    cases = live.select_cases()
    if mutation == 'split': cases[0] = cases[0].model_copy(update={'split': 'held_out'})
    elif mutation == 'answer': cases[0] = cases[0].model_copy(update={'answer': 'Changed'})
    elif mutation == 'label': cases[0] = cases[0].model_copy(update={'expected_action': 'MOVE_ON'})
    elif mutation == 'order': cases.reverse()
    elif mutation == 'duplicate': cases[1] = cases[0]
    elif mutation == 'subset': cases.pop()
    else: cases.append(cases[0])
    def forbidden(*args, **kwargs): raise AssertionError('Provider constructed before validation')
    monkeypatch.setattr(live, 'MatchedStage2Service', forbidden)
    with pytest.raises(ValueError): asyncio.run(live.experiment_report(cases))


def test_review_and_service_identity_guards(monkeypatch):
    cases = live.select_cases()
    with pytest.raises(ValueError): asyncio.run(live.experiment_report(cases, {}))
    swapped = {provider.SUPER_MODEL: provider.MatchedStage2Service(provider.ULTRA_MODEL),
               provider.ULTRA_MODEL: provider.MatchedStage2Service(provider.SUPER_MODEL)}
    with pytest.raises(ValueError): asyncio.run(live.experiment_report(cases, swapped))
    monkeypatch.setattr(live, 'reviewed_challenge_expectations', lambda _: (_ for _ in ()).throw(ValueError('Review invalid')))
    with pytest.raises(ValueError): asyncio.run(live.experiment_report(cases))


@pytest.mark.parametrize('args', [[], ['--live', '--split', 'held_out'], ['--live', '--case-id', 'other'],
    ['--live', '--model', 'other'], ['--live', '--reasoning-budget', '256'], ['--live', '--repeats', '2']])
def test_cli_rejects_unapproved_scope_before_provider_construction(monkeypatch, args):
    monkeypatch.setattr(sys, 'argv', ['stage2_model_match', *args])
    monkeypatch.setattr(live, 'MatchedStage2Service', lambda *_: pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit) as caught: live.main()
    assert caught.value.code == 2


def test_cli_unavailable_and_dns_failure_zero_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['stage2_model_match', '--live'])
    monkeypatch.setattr(live, 'MatchedStage2Service', lambda *_: pytest.fail('Provider constructed'))
    with pytest.raises(SystemExit): live.main()
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE_KEY')
    monkeypatch.setattr(live, 'dns_precheck', lambda: {'hostname_resolution': 'failure', 'elapsed_ms': 1})
    live.main()
    result = json.loads(capsys.readouterr().out)
    assert result['provider_requests'] == 0 and result['error'] == 'environment_network_restriction'


def test_dns_projection_hides_addresses_and_errors(monkeypatch):
    monkeypatch.setattr(live.socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('PRIVATE_IP', 443))]*2)
    result = live.dns_precheck()
    assert result['number_of_resolved_addresses'] == 1 and 'PRIVATE' not in json.dumps(result)
    def fail(*a, **k): raise OSError('PRIVATE_EXCEPTION')
    monkeypatch.setattr(live.socket, 'getaddrinfo', fail)
    assert live.dns_precheck()['hostname_resolution'] == 'failure'


def test_documentation_historical_scope_and_readonly_imports():
    doc = Path(live.__file__).with_name('STAGE2_MODEL_MATCH.md').read_text()
    for text in ('reasoning_budget=256', 'both', 'Historical Super', 'not its direct control',
                 'not a synthesized Stage 1 result', '30-second', 'production combined',
                 'MODEL-SELECTION PROMISING', 'MODEL-SELECTION NOT SUPPORTED', 'INCONCLUSIVE'):
        assert text in doc
    for module in (provider, live):
        tree = ast.parse(Path(module.__file__).read_text())
        assert not any(isinstance(node, ast.Global) for node in ast.walk(tree))
        assert not any(isinstance(node, ast.Attribute) and node.attr in ('write_text', 'write_bytes') for node in ast.walk(tree))
    assert live.ANNOTATIONS_SHA256 == hashlib.sha256(reviewed_challenge.ANNOTATIONS.read_bytes()).hexdigest()

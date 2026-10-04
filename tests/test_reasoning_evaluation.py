import asyncio
from dataclasses import asdict, replace
from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import httpx

from app.nemotron import NemotronInterviewerService
from app.reasoning import Decision, InvalidDecision, ProviderFailure, ReasoningFailed, ReasoningTimeout

# pytest adds backend/ to its path; load the standalone harness without installing a package.
spec = importlib.util.spec_from_file_location('rehearse_eval', Path(__file__).parents[1] / 'evals/interviewer/evaluate.py')
evaluation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluation
spec.loader.exec_module(evaluation)


def test_dataset_coverage_and_split():
    cases = evaluation.load_cases()
    assert len(cases) == 80
    assert Counter(c.split for c in cases) == {'development': 48, 'held_out': 32}
    assert {c.category for c in cases} == set(evaluation.CATEGORIES)
    for split in ('development', 'held_out'):
        assert {c.expected_action for c in cases if c.split == split} == set(evaluation.ACTIONS)
    for case in cases:
        assert set(case.context().model_dump()) == {'question', 'current_prompt', 'prior_turns', 'answer'}


def test_metrics_count_failures_and_per_action_errors():
    Outcome = evaluation.Outcome
    rows = [
        Outcome('a', 'vague', 'FOLLOW_UP', 'FOLLOW_UP', None, 10),
        Outcome('b', 'vague', 'FOLLOW_UP', 'MOVE_ON', None, 20),
        Outcome('c', 'ambiguous', 'CLARIFY', None, 'invalid_output', 30),
        Outcome('d', 'tradeoffs', 'CHALLENGE', None, 'provider_error', 40),
        Outcome('e', 'complete', 'MOVE_ON', None, 'timeout', 50),
        Outcome('f', 'complete', 'MOVE_ON', 'MOVE_ON', None, 60),
    ]
    result = evaluation.metrics(rows)
    assert result['attempted'] == 6
    assert result['action_match_rate'] == pytest.approx(2/6)
    assert result['per_action']['FOLLOW_UP'] == {'support': 2, 'precision': 1, 'recall': .5, 'f1': 2/3}
    assert result['per_action']['MOVE_ON'] == {'support': 2, 'precision': .5, 'recall': .5, 'f1': .5}
    assert result['macro_f1'] == pytest.approx((2/3 + .5)/4)
    assert result['confusion_matrix']['FOLLOW_UP']['MOVE_ON'] == 1
    assert result['confusion_matrix']['CLARIFY']['ERROR'] == 1
    for kind in ('invalid_output', 'provider_error', 'timeout'):
        assert result[f'{kind}_rate'] == pytest.approx(1/6)
    assert result['median_latency_ms'] == 35
    assert result['p95_latency_ms'] == 60
    assert result['by_category']['vague']['action_match_rate'] == .5
    assert result['repeat_agreement'] is None
    diagnosed = [replace(row, failure_kind='http_status', http_status=429)
                 if row.error == 'provider_error' else row for row in rows]
    assert evaluation.metrics(diagnosed) == result
    diagnosed = [replace(row, invalid_reason='schema_validation')
                 if row.error == 'invalid_output' else row for row in rows]
    assert evaluation.metrics(diagnosed) == result


@pytest.mark.parametrize('kind,status,error', [
    *[('http_status', status, 'provider_error') for status in (429, 401, 403, 500, 503)],
    ('transport', None, 'provider_error'),
    ('unavailable', None, 'provider_error'),
    ('request_size', None, 'provider_error'),
    ('adapter_error', None, 'provider_error'),
    (None, None, 'invalid_output'),
    (None, None, 'timeout'),
    (None, None, None),
])
def test_evaluator_sanitized_adapter_diagnostics(monkeypatch, capsys, kind, status, error):
    import app.nemotron as adapter
    monkeypatch.setenv('NVIDIA_API_KEY', 'PRIVATE-TEST-KEY')
    if kind == 'unavailable':
        monkeypatch.delenv('NVIDIA_API_KEY')
    if kind == 'request_size':
        monkeypatch.setattr(adapter, 'MAX_REQUEST_BYTES', 1)
    calls = []

    def handler(request):
        calls.append(True)
        if kind in ('unavailable', 'request_size'):
            pytest.fail('Local failure must not reach provider')
        if status:
            return httpx.Response(status, text='PRIVATE BODY', headers={'Authorization': 'PRIVATE HEADER'})
        if kind == 'transport':
            raise httpx.ConnectError('PRIVATE TRANSPORT ' + request.headers['Authorization'])
        if kind == 'adapter_error':
            raise RuntimeError('PRIVATE ADAPTER ' + request.headers['Authorization'])
        if error == 'timeout':
            raise httpx.ReadTimeout('PRIVATE TIMEOUT')
        message = ([] if error == 'invalid_output' else
                   {'role': 'assistant', 'content': json.dumps({
                       'action': 'CHALLENGE', 'reason': 'PRIVATE REASON', 'next_prompt': 'PRIVATE PROMPT'}),
                    'reasoning_content': 'PRIVATE TRACE'})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': message}]})

    case = next(c for c in evaluation.load_cases() if c.split == 'development')
    service = NemotronInterviewerService(httpx.MockTransport(handler))
    row = asyncio.run(evaluation.evaluate([case], service))[0]
    assert row.error == error
    assert row.failure_kind == kind
    assert row.http_status == status
    assert row.invalid_reason == ('response_envelope' if error == 'invalid_output' else None)
    assert row.predicted == ('CHALLENGE' if error is None else None)
    assert len(calls) == (0 if kind in ('unavailable', 'request_size') else 1)
    serialized = json.dumps(asdict(row))
    assert 'PRIVATE' not in serialized
    for content in (case.question, case.current_prompt, case.answer):
        assert content not in serialized
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('metadata', [
    {'failure_kind': 'unknown'},
    {'failure_kind': 'http_status'},
    {'failure_kind': 'transport', 'http_status': 429},
    {'failure_kind': 'http_status', 'http_status': '429'},
    {'failure_kind': 'http_status', 'http_status': 99},
    {'failure_kind': 'http_status', 'http_status': 600},
    {'failure_kind': 'transport', 'body': 'PRIVATE'},
])
def test_provider_failure_metadata_rejects_unsafe_fields(metadata):
    with pytest.raises(ValueError):
        ProviderFailure(**metadata)


def test_repeat_agreement_includes_failures():
    Outcome = evaluation.Outcome
    rows = [Outcome('a', 'vague', 'FOLLOW_UP', 'FOLLOW_UP', None, 0),
            Outcome('a', 'vague', 'FOLLOW_UP', 'FOLLOW_UP', None, 0, 1),
            Outcome('b', 'vague', 'FOLLOW_UP', None, 'timeout', 0),
            Outcome('b', 'vague', 'FOLLOW_UP', None, 'timeout', 0, 1)]
    assert evaluation.metrics(rows)['repeat_agreement'] == .5
    assert evaluation.metrics([])['p95_latency_ms'] is None


def test_harness_uses_only_context_and_never_records_text():
    cases = evaluation.load_cases()[:4]
    class Fake:
        count = 0
        async def decide(self, context):
            self.count += 1
            assert not hasattr(context, 'expected_action')
            if self.count == 1:
                return Decision(action='MOVE_ON', reason='private reason', next_prompt=None)
            raise [InvalidDecision(), ReasoningFailed(), ReasoningTimeout()][self.count-2]
    rows = asyncio.run(evaluation.evaluate(cases, Fake()))
    assert [r.error for r in rows] == [None, 'invalid_output', 'provider_error', 'timeout']
    assert 'private reason' not in repr(rows)
    assert cases[0].answer not in repr(rows)


def test_cli_requires_explicit_live_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['evaluate'])
    with pytest.raises(SystemExit) as caught:
        evaluation.main()
    assert caught.value.code == 2
    assert 'No requests made' in capsys.readouterr().err


@pytest.mark.parametrize('selection,message', [
    (['--show-decisions'], 'requires --live'),
    (['--live', '--show-decisions'], 'requires --case-id'),
    (['--live', '--show-decisions', '--split', 'development'], 'requires --case-id'),
    (['--live', '--show-decisions', '--case-id', 'development', '--split', 'held_out'], 'development-only'),
    (['--live', '--show-decisions', '--case-id', 'held_out'], 'exactly one development case'),
    (['--live', '--show-decisions', '--case-id', 'missing'], 'Unknown case ID'),
])
def test_cli_inspection_guards_before_provider(monkeypatch, capsys, selection, message):
    cases = evaluation.load_cases()
    ids = {split: next(c.id for c in cases if c.split == split)
           for split in ('development', 'held_out')}
    selection = [ids.get(value, value) if index and selection[index - 1] == '--case-id' else value
                 for index, value in enumerate(selection)]

    def forbidden(*args, **kwargs):
        pytest.fail('Rejected inspection must not construct a provider')

    monkeypatch.setenv('NVIDIA_API_KEY', 'TEST-KEY-NOT-REAL')
    monkeypatch.setattr(evaluation, 'NemotronInterviewerService', forbidden)
    monkeypatch.setattr(sys, 'argv', ['evaluate', *selection])
    with pytest.raises(SystemExit) as caught:
        evaluation.main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert message in captured.err
    assert 'No requests made' in captured.err


@pytest.mark.parametrize('action', ['CHALLENGE', 'MOVE_ON'])
def test_cli_inspection_projection_and_default_output_unchanged(monkeypatch, capsys, action):
    case = next(c for c in evaluation.load_cases() if c.id == 'multi_turn-001')
    decision = {'action': action, 'reason': 'Examine the conclusion.',
                'next_prompt': None if action == 'MOVE_ON' else 'What supports that conclusion?'}
    hidden = 'UNIQUE-HIDDEN-TRACE-7b4e'
    key = 'TEST-SECRET-KEY-7b4e'
    calls = []

    def handler(request):
        calls.append(True)
        assert request.headers['Authorization'] == f'Bearer {key}'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(decision), 'reasoning_content': hidden,
            'reasoning': hidden}}], 'provider_internal': 'UNIQUE-PROVIDER-INTERNAL'},
            headers={'X-Private': 'UNIQUE-HEADER'})

    monkeypatch.setenv('NVIDIA_API_KEY', key)
    monkeypatch.setattr(evaluation, 'NemotronInterviewerService',
                        lambda *, config: NemotronInterviewerService(httpx.MockTransport(handler), config))
    monkeypatch.setattr(evaluation.time, 'perf_counter', lambda: 1.0)
    base_args = ['evaluate', '--live', '--case-id', case.id, '--split', 'development', '--repeats', '2']
    monkeypatch.setattr(sys, 'argv', base_args)
    evaluation.main()
    default_text = capsys.readouterr().out
    default = json.loads(default_text)
    assert set(default) == {'dataset_version', 'dataset_sha256', 'prompt_version', 'config_version',
                            'model', 'config', 'split', 'repeats', 'metrics', 'outcomes'}
    for row in default['outcomes']:
        assert set(row) == {'case_id', 'category', 'expected', 'predicted', 'error', 'latency_ms',
                            'repeat', 'failure_kind', 'http_status', 'invalid_reason', 'json_reason'}
    assert decision['reason'] not in default_text
    assert 'next_prompt' not in default_text
    monkeypatch.setattr(sys, 'argv', [*base_args, '--show-decisions'])
    evaluation.main()
    inspected_text = capsys.readouterr().out
    inspected = json.loads(inspected_text)
    inspection = inspected.pop('decision_inspection')
    assert json.dumps(inspected, indent=2) + '\n' == default_text
    assert inspection == [{'case_id': case.id, 'expected': case.expected_action,
                           'predicted': action, 'reason': decision['reason'],
                           'next_prompt': decision['next_prompt'], 'repeat': repeat} for repeat in range(2)]
    for secret in (hidden, key, f'Bearer {key}', 'UNIQUE-PROVIDER-INTERNAL', 'UNIQUE-HEADER',
                   case.question, case.current_prompt, case.answer,
                   *[text for turn in case.prior_turns for text in (turn.prompt, turn.answer)]):
        assert secret not in inspected_text
    for field in ('answer', 'question', 'current_prompt', 'prior_turns', 'reasoning_content',
                  'Authorization', 'NVIDIA_API_KEY', 'messages', 'headers'):
        assert f'"{field}"' not in inspected_text
    assert len(calls) == 4


@pytest.mark.parametrize('failure,error', [('http', 'provider_error'), ('transport', 'provider_error'),
                                         ('timeout', 'timeout'), ('invalid', 'invalid_output')])
def test_cli_failed_inspection_excludes_decision_text(monkeypatch, capsys, failure, error):
    case = next(c for c in evaluation.load_cases() if c.split == 'development')
    marker = 'UNIQUE-PRIVATE-FAILURE-CONTENT'

    def handler(request):
        if failure == 'transport':
            raise httpx.ConnectError(marker + request.headers['Authorization'])
        if failure == 'timeout':
            raise httpx.ReadTimeout(marker)
        if failure == 'http':
            return httpx.Response(429, text=marker, headers={'Authorization': marker})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps({'action': 'CHALLENGE', 'reason': marker,
                'next_prompt': marker, 'unexpected': marker}), 'reasoning_content': marker}}]})

    monkeypatch.setenv('NVIDIA_API_KEY', 'TEST-FAILURE-KEY')
    monkeypatch.setattr(evaluation, 'NemotronInterviewerService',
                        lambda *, config: NemotronInterviewerService(httpx.MockTransport(handler), config))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--live', '--case-id', case.id, '--show-decisions'])
    evaluation.main()
    text = capsys.readouterr().out
    report = json.loads(text)
    assert report['decision_inspection'] == []
    assert report['outcomes'][0]['error'] == error
    assert report['outcomes'][0]['invalid_reason'] == ('schema_validation' if failure == 'invalid' else None)
    assert report['metrics'][f'{error}_rate'] == 1.0
    assert marker not in text
    assert 'TEST-FAILURE-KEY' not in text
    assert '"reason"' not in text and '"next_prompt"' not in text
    if failure == 'http':
        assert report['outcomes'][0]['failure_kind'] == 'http_status'
        assert report['outcomes'][0]['http_status'] == 429


def test_inspection_revalidates_constructed_decisions():
    case = next(c for c in evaluation.load_cases() if c.split == 'development')
    class Fake:
        async def decide(self, context):
            return Decision.model_construct(action='CHALLENGE', reason='PRIVATE', next_prompt=None)
    inspection = []
    rows = asyncio.run(evaluation.evaluate([case], Fake(), on_decision=inspection.append))
    assert rows[0].error == 'invalid_output'
    assert inspection == []


@pytest.mark.parametrize('split', [None, 'development', 'held_out'])
def test_cli_selects_exact_case(monkeypatch, capsys, split):
    case = next(c for c in evaluation.load_cases() if c.split == (split or 'held_out'))
    _run_fake_cli(monkeypatch, ['--case-id', case.id] + (['--split', split] if split else []))
    report = json.loads(capsys.readouterr().out)
    assert [row['case_id'] for row in report['outcomes']] == [case.id]
    assert report['metrics']['attempted'] == 1
    assert report['split'] == case.split


@pytest.mark.parametrize('split', [None, 'development', 'held_out'])
def test_cli_without_case_id_preserves_split(monkeypatch, capsys, split):
    _run_fake_cli(monkeypatch, ['--split', split] if split else [])
    report = json.loads(capsys.readouterr().out)
    expected = [c.id for c in evaluation.load_cases() if c.split == (split or 'development')]
    assert [row['case_id'] for row in report['outcomes']] == expected
    assert report['split'] == (split or 'development')


def _run_fake_cli(monkeypatch, selection):
    class Fake:
        def __init__(self, *, config):
            pass

        async def decide(self, context):
            return Decision(action='MOVE_ON', reason='Offline test.', next_prompt=None)

    monkeypatch.setenv('NVIDIA_API_KEY', 'offline-test-placeholder')
    monkeypatch.setattr(evaluation, 'NemotronInterviewerService', Fake)
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--live', *selection])
    evaluation.main()


@pytest.mark.parametrize('kind', ['nonexistent', 'split_mismatch'])
@pytest.mark.parametrize('configured_key', [False, True])
def test_cli_invalid_selection_never_constructs_provider(monkeypatch, capsys, kind, configured_key):
    def forbidden_provider(*args, **kwargs):
        pytest.fail('Invalid selection must fail before constructing a provider')

    case = next(c for c in evaluation.load_cases() if c.split == 'held_out')
    selection = (['--case-id', 'does-not-exist'] if kind == 'nonexistent'
                 else ['--case-id', case.id, '--split', 'development'])
    if configured_key:
        monkeypatch.setenv('NVIDIA_API_KEY', 'offline-test-placeholder')
    monkeypatch.setattr(evaluation, 'NemotronInterviewerService', forbidden_provider)
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--live', *selection])
    with pytest.raises(SystemExit) as caught:
        evaluation.main()
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert ('Unknown case ID: does-not-exist' if kind == 'nonexistent'
            else f'Case {case.id} belongs to held_out, not development') in error
    assert 'No requests made' in error


@pytest.mark.parametrize('kind', ['duplicate_id', 'duplicate_context', 'invalid_action', 'extra', 'bad_category'])
def test_dataset_rejects_invalid_cases(tmp_path, kind):
    import json
    rows = [c.model_dump(mode='json') for c in evaluation.load_cases()[:2]]
    if kind == 'duplicate_id':
        rows[1]['id'] = rows[0]['id']
    elif kind == 'duplicate_context':
        rows[1] = {**rows[0], 'id': rows[1]['id'], 'split': 'held_out'}
    elif kind == 'invalid_action':
        rows[0]['expected_action'] = 'CHAT'
    elif kind == 'extra':
        rows[0]['extra'] = True
    else:
        rows[0]['category'] = 'unknown'
    path = tmp_path / 'cases.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError):
        evaluation.load_cases(path)

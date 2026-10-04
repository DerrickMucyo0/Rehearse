import asyncio
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

from app.audio import MAX_AUDIO_BYTES, MAX_BODY_BYTES
from app.main import app
from app.session_routes import get_session_service
from app.sessions import InterviewSessionService
from conftest import advance, answer_payload
from app.transcription import (
    ElevenLabsTranscriptionService, TranscriptionFailed, TranscriptionResult,
    TranscriptionTimeout, TranscriptionUnavailable, get_transcription_service,
)


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    monkeypatch.delenv('ELEVENLABS_API_KEY', raising=False)

    async def blocked(*args, **kwargs):
        raise AssertionError('Real provider network is forbidden in tests')

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', blocked)


class FakeTranscriber:
    def __init__(self):
        self.calls = []
        self.error = None
        self.after = None

    async def transcribe(self, audio, filename):
        self.calls.append((audio, filename, await audio.read()))
        if self.error:
            raise self.error
        if self.after:
            self.after()
        return TranscriptionResult(text='Hello there', language='eng', words=[
            {'text': 'Hello', 'start': 0.0, 'end': 0.5},
        ])


@pytest.fixture
def setup():
    service = InterviewSessionService()
    fake = FakeTranscriber()
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_transcription_service] = lambda: fake
    try:
        with TestClient(app) as client:
            yield client, service, fake
    finally:
        app.dependency_overrides.clear()


def post(client, session_id, index='0', audio=b'fake audio', mime='audio/webm;codecs=opus'):
    return client.post(f'/api/sessions/{session_id}/transcriptions', data={'question_index': index, 'turn_revision': index},
                       files={'audio': ('../../private.webm', audio, mime)})


def test_success_does_not_advance_and_closes_file(setup):
    client, service, fake = setup
    session = service.start()
    result = post(client, session.id)
    assert result.status_code == 200
    assert result.json() == {'session_id': str(session.id), 'question_index': 0, 'turn_revision': 0,
                             'text': 'Hello there', 'language': 'eng',
                             'words': [{'text': 'Hello', 'start': 0.0, 'end': 0.5}]}
    assert service.get(session.id) == session
    assert fake.calls[0][1:] == ('answer-1.webm', b'fake audio')
    assert fake.calls[0][0].file.closed
    assert client.post(f'/api/sessions/{session.id}/answers', json=answer_payload(0, 'Edited transcript')).json()['current_question_index'] == 1


def test_unknown_session(setup):
    client, _, fake = setup
    assert post(client, uuid4()).status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize('index', ['0', '2'])
def test_wrong_question(setup, index):
    client, service, fake = setup
    session = service.start()
    advance(service, session.id, 0, 'First')
    assert post(client, session.id, index=index).status_code == 409
    assert fake.calls == []


def test_completed_session(setup):
    client, service, fake = setup
    session = service.start()
    for index in range(5):
        advance(service, session.id, index, 'Answer')
    assert post(client, session.id, index='5').status_code == 409
    assert fake.calls == []


@pytest.mark.parametrize(('audio', 'mime', 'status'), [
    (b'', 'audio/webm', 422), (b'x', 'text/plain', 415),
    (b'x' * (MAX_AUDIO_BYTES + 1), 'audio/webm', 413),
])
def test_invalid_audio_never_calls_provider(setup, audio, mime, status):
    client, service, fake = setup
    assert post(client, service.start().id, audio=audio, mime=mime).status_code == status
    assert fake.calls == []


def test_request_body_bound_without_content_length(setup):
    client, service, fake = setup
    response = client.post(f'/api/sessions/{service.start().id}/transcriptions',
                           content=iter([b'x' * (MAX_BODY_BYTES + 1)]),
                           headers={'content-type': 'multipart/form-data; boundary=test'})
    assert response.status_code == 413
    assert fake.calls == []


@pytest.mark.parametrize('index', ['-1', 'wrong', '1.5'])
def test_invalid_question_index(setup, index):
    client, service, fake = setup
    assert post(client, service.start().id, index=index).status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(('error', 'status'), [
    (TranscriptionFailed('sensitive provider detail'), 502),
    (TranscriptionTimeout('sensitive provider detail'), 504),
    (TranscriptionUnavailable('sensitive provider detail'), 503),
])
def test_controlled_errors_close_upload_and_preserve_session(setup, error, status):
    client, service, fake = setup
    session = service.start()
    fake.error = error
    result = post(client, session.id)
    assert result.status_code == status
    assert 'sensitive' not in result.text
    assert fake.calls[0][0].file.closed
    assert service.get(session.id) == session


def test_missing_configuration_uses_real_adapter_without_network(setup):
    client, service, _ = setup
    app.dependency_overrides.pop(get_transcription_service)
    result = post(client, service.start().id)
    assert result.status_code == 503
    assert result.json() == {'detail': 'Transcription is not configured on the server.'}


def test_question_advanced_during_provider_call(setup):
    client, service, fake = setup
    session = service.start()
    fake.after = lambda: advance(service, session.id, 0, 'Other tab')
    assert post(client, session.id).status_code == 409
    assert fake.calls[0][0].file.closed
    assert service.get(session.id).answers == ['Other tab']


def provider_response(**changes):
    result = {'text': 'Hello there', 'language_code': 'eng', 'language_probability': 0.99,
              'words': [{'text': 'Hello', 'start': 0.0, 'end': 0.5, 'type': 'word', 'logprob': -0.1},
                        {'text': ' ', 'start': 0.5, 'end': 0.6, 'type': 'spacing', 'logprob': -0.1}],
              'transcription_id': 'not-exposed'}
    result.update(changes)
    return result


def run_adapter(monkeypatch, handler):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-only-not-a-real-key')
    audio = UploadFile(BytesIO(b'fake audio'), headers=Headers({'content-type': 'audio/webm;codecs=opus'}))

    async def run():
        try:
            return await ElevenLabsTranscriptionService(httpx.MockTransport(handler)).transcribe(audio, 'answer-1.webm')
        finally:
            await audio.close()
    return asyncio.run(run())


def test_official_sdk_request_and_timing_mapping(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == '/v1/speech-to-text'
        assert request.headers['xi-api-key'] == 'test-only-not-a-real-key'
        body = request.read()
        assert b'scribe_v2' in body
        assert b'name="timestamps_granularity"\r\n\r\nword' in body
        assert b'filename="answer-1.webm"' in body
        assert b'audio/webm;codecs=opus' in body
        return httpx.Response(200, json=provider_response())

    result = run_adapter(monkeypatch, handler)
    assert len(calls) == 1
    assert result.model_dump() == {'text': 'Hello there', 'language': 'eng',
                                   'words': [{'text': 'Hello', 'start': 0.0, 'end': 0.5}]}


@pytest.mark.parametrize('body', [
    {}, provider_response(text=''), provider_response(text='  '), provider_response(text=None),
    provider_response(text='x' * 10001), provider_response(words=[{'type': 'word', 'text': 'x', 'start': -1, 'end': 0}]),
    provider_response(words=[{'type': 'word', 'text': 'x', 'start': 2, 'end': 1}]),
])
def test_malformed_or_empty_provider_result(monkeypatch, body):
    with pytest.raises(TranscriptionFailed):
        run_adapter(monkeypatch, lambda _: httpx.Response(200, json=body))


def test_optional_language_and_timings(monkeypatch):
    result = run_adapter(monkeypatch, lambda _: httpx.Response(200, json=provider_response(
        language_code=None, words=[{'type': 'word', 'text': 'Hello', 'start': None, 'end': None}],
    )))
    assert result.text == 'Hello there'
    assert result.language is None
    assert result.words == []


@pytest.mark.parametrize('status', [401, 429, 500])
def test_provider_http_failure_not_retried_or_exposed(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'detail': 'sensitive provider detail'})

    with pytest.raises(TranscriptionFailed) as failure:
        run_adapter(monkeypatch, handler)
    assert str(failure.value) == ''
    assert len(calls) == 1


def test_provider_network_timeout(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout('sensitive detail', request=request)
    with pytest.raises(TranscriptionTimeout):
        run_adapter(monkeypatch, handler)


def test_total_provider_deadline(monkeypatch):
    import app.transcription as module
    monkeypatch.setattr(module, 'PROVIDER_TIMEOUT_SECONDS', 0.01)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=provider_response())

    with pytest.raises(TranscriptionTimeout):
        run_adapter(monkeypatch, handler)


@pytest.mark.parametrize('body', [provider_response(text=''), {'detail': 'private provider data'}])
def test_real_adapter_failures_reach_route_as_sanitized_errors(setup, monkeypatch, body):
    client, service, _ = setup
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-only-not-a-real-key')
    adapter = ElevenLabsTranscriptionService(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    app.dependency_overrides[get_transcription_service] = lambda: adapter
    result = post(client, service.start().id)
    assert result.status_code == 502
    assert result.json() == {'detail': 'Unable to transcribe this recording. Try again or type your answer.'}


@pytest.mark.parametrize('flag', [None, '0', 'true'])
def test_diagnostics_off_unless_explicitly_enabled(monkeypatch, capsys, flag):
    if flag is None:
        monkeypatch.delenv('REHEARSE_TRANSCRIPTION_DEBUG', raising=False)
    else:
        monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', flag)
    with pytest.raises(TranscriptionFailed):
        run_adapter(monkeypatch, lambda _: httpx.Response(401, json={'detail': 'private'}))
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize(('status', 'detail', 'category'), [
    (401, {}, 'authentication'), (403, {}, 'authorization'),
    (402, {}, 'payment_or_quota'), (400, {'status': 'quota_exceeded'}, 'payment_or_quota'),
    (422, {}, 'validation'), (429, {}, 'rate_limit'), (500, {}, 'provider_server_error'),
    (418, {'code': 'private-code', 'type': 'private-type'}, 'unknown_provider_error'),
])
def test_diagnostics_only_log_safe_fields(monkeypatch, capsys, status, detail, category):
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')
    body = {'detail': {**detail, 'message': 'PRIVATE-BODY', 'audio': 'PRIVATE-AUDIO',
                       'transcript': 'PRIVATE-TRANSCRIPT'}}
    with pytest.raises(TranscriptionFailed):
        run_adapter(monkeypatch, lambda _: httpx.Response(status, json=body, headers={'secret': 'PRIVATE-HEADER'}))
    output = capsys.readouterr().err
    assert output == f'transcription_failure stage=provider_request provider_status={status} category={category}\n'
    for secret in ['test-only-not-a-real-key', 'PRIVATE-', 'private-code', 'private-type', 'fake audio']:
        assert secret not in output


def test_mapping_diagnostic_does_not_log_transcript(monkeypatch, capsys):
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')
    with pytest.raises(TranscriptionFailed):
        run_adapter(monkeypatch, lambda _: httpx.Response(200, json=provider_response(text='PRIVATE-TRANSCRIPT' * 1000)))
    output = capsys.readouterr().err
    assert output == 'transcription_failure stage=result_mapping provider_status=unavailable category=validation\n'
    assert 'PRIVATE-' not in output


@pytest.mark.parametrize(('kind', 'status', 'message'), [
    ('missing', 503, 'Transcription is not configured on the server.'),
    ('timeout', 504, 'Transcription timed out. Please try again.'),
    ('api', 502, 'Unable to transcribe this recording. Try again or type your answer.'),
    ('network', 502, 'Unable to transcribe this recording. Try again or type your answer.'),
])
def test_debug_keeps_http_contract_and_hides_exception_details(setup, monkeypatch, capsys, kind, status, message):
    client, service, _ = setup
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')
    if kind != 'missing':
        monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-only-not-a-real-key')

    def handler(request):
        if kind == 'timeout':
            raise httpx.ReadTimeout('PRIVATE-EXCEPTION', request=request)
        if kind == 'network':
            raise httpx.ConnectError('PRIVATE-EXCEPTION', request=request)
        return httpx.Response(403, json={'detail': {'message': 'PRIVATE-BODY'}})

    app.dependency_overrides[get_transcription_service] = lambda: ElevenLabsTranscriptionService(httpx.MockTransport(handler))
    response = post(client, service.start().id)
    assert response.status_code == status
    assert response.json() == {'detail': message}
    output = capsys.readouterr().err
    assert 'PRIVATE-' not in output
    assert 'test-only-not-a-real-key' not in output
    assert len(output.splitlines()) == (0 if kind == 'missing' else 1)
    if kind in ('network', 'timeout'):
        assert output == 'transcription_failure stage=provider_request provider_status=unavailable category=network_error\n'


@pytest.mark.parametrize('flag', ['0', '1'])
@pytest.mark.parametrize('suppress_logging', [False, True])
def test_terminal_diagnostic_in_uvicorn_reload_child(tmp_path, flag, suppress_logging):
    """Use Uvicorn's actual spawn wrapper and OS stderr, without pytest log handlers."""
    import os
    from pathlib import Path
    import subprocess
    import sys

    script = tmp_path / 'reload_diagnostic_probe.py'
    script.write_text('''
import asyncio
from io import BytesIO
import logging
import os
import httpx
from starlette.datastructures import Headers, UploadFile
from uvicorn import Config
from uvicorn._subprocess import get_subprocess
from app.transcription import ElevenLabsTranscriptionService, TranscriptionFailed

def child(sockets):
    if os.environ['SUPPRESS_LOGGING'] == '1':
        logging.getLogger().addHandler(logging.NullHandler())
        logging.disable(logging.CRITICAL)
    async def run():
        audio = UploadFile(BytesIO(b'PRIVATE-AUDIO'), headers=Headers({'content-type': 'audio/webm'}))
        transport = httpx.MockTransport(lambda request: httpx.Response(403,
            json={'detail': {'type': 'authorization_error', 'message': 'PRIVATE-BODY'}},
            headers={'secret': 'PRIVATE-HEADER'}))
        try:
            await ElevenLabsTranscriptionService(transport).transcribe(audio, 'safe.webm')
        except TranscriptionFailed:
            pass
        finally:
            await audio.close()
    asyncio.run(run())

if __name__ == '__main__':
    process = get_subprocess(config=Config('app.main:app', reload=True), target=child, sockets=[])
    process.start()
    process.join(15)
    if process.is_alive():
        process.terminate()
        process.join()
        raise SystemExit(1)
    raise SystemExit(process.exitcode)
''')
    result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True,
                            timeout=25, env={
                                'PATH': os.defpath,
                                'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'backend'),
                                'PYTHONDONTWRITEBYTECODE': '1',
                                'ELEVENLABS_API_KEY': 'test-only-not-a-real-key',
                                'REHEARSE_TRANSCRIPTION_DEBUG': flag,
                                'SUPPRESS_LOGGING': '1' if suppress_logging else '0',
                            })
    assert result.returncode == 0
    assert result.stdout == ''
    expected = 'transcription_failure stage=provider_request provider_status=403 category=authorization\n'
    # Uvicorn also emits its normal reload-directory announcement.
    diagnostic_output = ''.join(line + '\n' for line in result.stderr.splitlines()
                                if not line.startswith('INFO:     Will watch for changes in these directories:'))
    assert diagnostic_output == (expected if flag == '1' else '')
    assert 'PRIVATE-' not in result.stderr
    assert 'test-only-not-a-real-key' not in result.stderr


def test_deadline_emits_exactly_one_terminal_line(monkeypatch, capsys):
    import app.transcription as module
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')
    monkeypatch.setattr(module, 'PROVIDER_TIMEOUT_SECONDS', 0.01)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=provider_response())

    with pytest.raises(TranscriptionTimeout):
        run_adapter(monkeypatch, handler)
    assert capsys.readouterr().err == 'transcription_failure stage=provider_request provider_status=unavailable category=network_error\n'


def test_outer_failure_boundary_cannot_skip_diagnostic(monkeypatch, capsys):
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')

    async def fail_before_provider(*args):
        raise RuntimeError('PRIVATE-EXCEPTION')

    monkeypatch.setattr(ElevenLabsTranscriptionService, '_convert', fail_before_provider)
    with pytest.raises(TranscriptionFailed):
        run_adapter(monkeypatch, lambda _: httpx.Response(200, json=provider_response()))
    assert capsys.readouterr().err == 'transcription_failure stage=provider_request provider_status=unavailable category=unknown_provider_error\n'


@pytest.mark.parametrize('outcome', ['provider_failure', 'mapping_failure', 'success', 'invalid_audio'])
def test_actual_route_default_dependency(setup, monkeypatch, capsys, outcome):
    import app.transcription as module
    client, service, _ = setup
    # Exercise the actual default factory, constructor and adapter, not a fake service.
    app.dependency_overrides.pop(get_transcription_service)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-only-not-a-real-key')
    monkeypatch.setenv('REHEARSE_TRANSCRIPTION_DEBUG', '1')
    original_client = httpx.AsyncClient

    def handler(request):
        if outcome == 'provider_failure':
            return httpx.Response(403, json={'detail': {'message': 'PRIVATE-BODY'}})
        return httpx.Response(200, json=provider_response(text='' if outcome == 'mapping_failure' else 'Hello'))

    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original_client(
        **{**kwargs, 'transport': httpx.MockTransport(handler)}))
    result = post(client, service.start().id, audio=b'' if outcome == 'invalid_audio' else b'PRIVATE-AUDIO')
    output = capsys.readouterr().err
    assert result.status_code == {'provider_failure': 502, 'mapping_failure': 502, 'success': 200, 'invalid_audio': 422}[outcome]
    assert output.count('transcription_failure ') == int(outcome in ('provider_failure', 'mapping_failure'))
    assert 'PRIVATE-' not in output
    assert 'test-only-not-a-real-key' not in output
    if outcome == 'provider_failure':
        assert 'stage=provider_request provider_status=403 category=authorization' in output
    if outcome == 'mapping_failure':
        assert 'stage=result_mapping provider_status=unavailable category=validation' in output


def test_same_question_turn_changes_during_transcription(setup):
    from app.reasoning import Decision
    from app.sessions import AnswerRequest
    client, service, fake = setup
    session = service.start()

    def follow_up():
        answer = AnswerRequest(**answer_payload(0, 'Concurrent typed answer'))
        service.begin_submission(session.id, answer)
        service.submit_answer(session.id, answer, Decision(
            action='FOLLOW_UP', reason='Missing result.', next_prompt='What was the result?'))

    fake.after = follow_up
    assert post(client, session.id).status_code == 409
    assert service.get(session.id).current_question_index == 0
    assert service.get(session.id).turn_revision == 1
    assert fake.calls[0][0].file.closed


def test_successful_transcription_for_follow_up_does_not_invoke_reasoning(setup):
    from app.reasoning import Decision
    from app.sessions import AnswerRequest
    from app.nemotron import get_reasoning_service
    client, service, fake = setup
    session = service.start()
    answer = AnswerRequest(**answer_payload(0, 'Typed answer'))
    service.begin_submission(session.id, answer)
    current = service.submit_answer(session.id, answer, Decision(
        action='CLARIFY', reason='Unclear.', next_prompt='Which project?'))

    class Forbidden:
        async def decide(self, context):
            raise AssertionError('Transcription must not invoke reasoning')
    app.dependency_overrides[get_reasoning_service] = lambda: Forbidden()
    response = client.post(f'/api/sessions/{session.id}/transcriptions',
        data={'question_index': '0', 'turn_revision': '1'},
        files={'audio': ('answer.webm', b'fake audio', 'audio/webm')})
    assert response.status_code == 200
    assert response.json()['turn_revision'] == 1
    assert service.get(session.id) == current

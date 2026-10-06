from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.audio import MAX_AUDIO_BYTES, MAX_BODY_BYTES
from app.main import app
from app.session_routes import get_session_service, get_transitional_provider_session_service
from app.sessions import InterviewSessionService


@pytest.fixture
def client(
    postgres_session_factory, authenticated_principal,
    authenticated_http_headers, authenticated_session_override,
) -> Iterator[TestClient]:
    service = InterviewSessionService(postgres_session_factory, authenticated_principal)
    app.dependency_overrides[get_session_service] = authenticated_session_override(service)
    app.dependency_overrides[get_transitional_provider_session_service] = lambda: service
    try:
        with TestClient(app, headers=authenticated_http_headers) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session_service)
        app.dependency_overrides.pop(get_transitional_provider_session_service)


def upload(client, session_id, index=0, content=b'audio bytes', content_type='audio/webm;codecs=opus'):
    return client.post(f'/api/sessions/{session_id}/audio', data={'question_index': str(index)},
                       files={'audio': ('../../private/recording.webm', content, content_type)})


def test_accepts_audio_without_mutating_session(client):
    session = client.post('/api/sessions').json()
    response = upload(client, session['id'])
    assert response.status_code == 200
    assert response.json() == {
        'session_id': session['id'], 'question_index': 0, 'filename': 'answer-1.webm',
        'content_type': 'audio/webm;codecs=opus', 'size_bytes': 11, 'status': 'accepted',
    }
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    submitted = client.post(f"/api/sessions/{session['id']}/questions/0/attempts", json={
        'expected_last_attempt_number': 0, 'answer': 'Typed answer still works',
    })
    assert submitted.status_code == 201
    assert submitted.json()['session']['current_question_index'] == 0
    assert client.post(f"/api/sessions/{session['id']}/questions/0/continue", json={
        'expected_last_attempt_number': 1,
    }).json()['current_question_index'] == 1


def test_unknown_session(client):
    assert upload(client, uuid4()).status_code == 404


@pytest.mark.parametrize('index', [0, 2])
def test_wrong_question(client, index):
    session = client.post('/api/sessions').json()
    assert client.post(f"/api/sessions/{session['id']}/questions/0/attempts", json={
        'expected_last_attempt_number': 0, 'answer': 'First',
    }).status_code == 201
    assert client.post(f"/api/sessions/{session['id']}/questions/0/continue", json={
        'expected_last_attempt_number': 1,
    }).status_code == 200
    assert upload(client, session['id'], index=index).status_code == 409


def test_completed_session(client):
    session = client.post('/api/sessions').json()
    for index in range(5):
        assert client.post(f"/api/sessions/{session['id']}/questions/{index}/attempts", json={
            'expected_last_attempt_number': 0, 'answer': 'Answer',
        }).status_code == 201
        assert client.post(f"/api/sessions/{session['id']}/questions/{index}/continue", json={
            'expected_last_attempt_number': 1,
        }).status_code == 200
    assert upload(client, session['id'], index=5).status_code == 409


@pytest.mark.parametrize(('content', 'content_type', 'status'), [
    (b'', 'audio/webm', 422), (b'bytes', 'text/plain', 415),
    (b'bytes', 'video/webm', 415), (b'x' * (MAX_AUDIO_BYTES + 1), 'audio/webm', 413),
    (b'x' * MAX_AUDIO_BYTES, 'audio/webm', 200),
])
def test_upload_validation(client, content, content_type, status):
    session = client.post('/api/sessions').json()
    assert upload(client, session['id'], content=content, content_type=content_type).status_code == status
    assert client.get(f"/api/sessions/{session['id']}").json() == session


@pytest.mark.parametrize('content_type', ['audio/ogg;codecs=opus', 'audio/mp4', 'audio/mpeg', 'audio/wav', 'audio/x-wav'])
def test_supported_types(client, content_type):
    session = client.post('/api/sessions').json()
    assert upload(client, session['id'], content_type=content_type).json()['content_type'] == content_type


@pytest.mark.parametrize('index', ['-1', 'x', '1.2', '', '99999999999'])
def test_invalid_index(client, index):
    session = client.post('/api/sessions').json()
    assert upload(client, session['id'], index=index).status_code == 422


def test_bounds_stream_without_content_length(client):
    session = client.post('/api/sessions').json()
    response = client.post(f"/api/sessions/{session['id']}/audio",
                           content=iter([b'x' * (MAX_BODY_BYTES + 1)]),
                           headers={'content-type': 'multipart/form-data; boundary=test'})
    assert response.status_code == 413


def test_missing_fields_and_non_multipart(client):
    session = client.post('/api/sessions').json()
    url = f"/api/sessions/{session['id']}/audio"
    assert client.post(url, json={}).status_code == 415
    assert client.post(url, files={'audio': ('a.webm', b'a', 'audio/webm')}).status_code == 422
    assert client.post(url, data={'question_index': '0'}, files={'wrong': ('a.webm', b'a', 'audio/webm')}).status_code == 422
    assert upload(client, 'not-a-uuid').status_code == 422


def test_multipart_field_and_file_limits(client):
    session = client.post('/api/sessions').json()
    url = f"/api/sessions/{session['id']}/audio"
    assert client.post(url, data={'question_index': '0', 'extra': 'x'},
                       files={'audio': ('a.webm', b'a', 'audio/webm')}).status_code == 400
    assert client.post(url, data={'question_index': '0'}, files=[
        ('audio', ('a.webm', b'a', 'audio/webm')), ('audio', ('b.webm', b'b', 'audio/webm')),
    ]).status_code == 400


def test_temporary_files_closed_on_success_and_rejection(client, monkeypatch):
    from starlette.datastructures import UploadFile

    closed = []
    original = UploadFile.close

    async def close(file):
        await original(file)
        closed.append(file.file.closed)

    monkeypatch.setattr(UploadFile, 'close', close)
    session = client.post('/api/sessions').json()
    assert upload(client, session['id']).status_code == 200
    assert upload(client, session['id'], content_type='text/plain').status_code == 415
    assert closed == [True, True]


def test_rechecks_current_question_after_transfer(client, monkeypatch):
    import app.session_routes as routes
    from app.sessions import AttemptRequest, ContinueRequest

    original = routes.bounded_multipart_request
    service = app.dependency_overrides[get_transitional_provider_session_service]()
    session = service.start()

    async def advance_during_transfer(request):
        bounded = await original(request)
        service.submit_attempt(session.id, 0, AttemptRequest(
            expected_last_attempt_number=0, answer='Concurrent answer',
        ))
        service.continue_question(session.id, 0, ContinueRequest(expected_last_attempt_number=1))
        return bounded

    monkeypatch.setattr(routes, 'bounded_multipart_request', advance_during_transfer)
    assert upload(client, session.id).status_code == 409
    assert service.get(session.id).answers == ['Concurrent answer']

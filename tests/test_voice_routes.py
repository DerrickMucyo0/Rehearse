"""Speech is an owned, read-only projection; every provider is offline."""

from dataclasses import dataclass
from datetime import timedelta
from threading import get_ident
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app import session_routes
from app.auth import VerifiedExternalIdentity
from app.auth_http import AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME, get_auth_session_store
from app.auth_persistence import PostgreSQLAuthSessionStore
from app.database_models import AuthSession, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.main import app
from app.roleplay import RoleplayQuestion
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService
from app.voice import SpeechFailed, SpeechTimeout, SpeechUnavailable, SynthesizedSpeech
from app.voice_composition import get_speech_service

AUDIO = b"offline-validated-audio-sentinel"
UNAVAILABLE = {"detail": "Voice playback is unavailable right now."}
NOT_FOUND = {"detail": "Session not found."}


def persisted(factory):
    with factory() as database:
        return {
            model.__tablename__: list(database.execute(
                select(model.__table__).order_by(model.__table__.c.id),
            ).mappings())
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


class Speaker:
    def __init__(self, engine):
        self.engine = engine
        self.texts = []
        self.during = None
        self.thread = None

    async def synthesize(self, text):
        self.thread = get_ident()
        self.texts.append(text)
        assert self.engine.pool.checkedout() == 0
        if self.during:
            self.during()
        assert self.engine.pool.checkedout() == 0
        return SynthesizedSpeech(audio=AUDIO)


@dataclass
class Harness:
    factory: object
    store: object
    login: object
    other_login: object
    speaker: Speaker
    client: TestClient

    @property
    def service(self):
        return InterviewSessionService(self.factory, self.login.principal)

    def speech(self, identifier, index=0, **kwargs):
        return self.client.post(f"/api/sessions/{identifier}/questions/{index}/speech", **kwargs)


@pytest.fixture
def harness(postgres_session_factory, postgres_engine, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Voice route tests must never contact a provider.")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    # Separate domain boundaries may not be invoked from this endpoint.
    monkeypatch.setattr(session_routes, "diagnose_application_context", forbidden_async)
    monkeypatch.setattr(session_routes, "continue_application_attempt", forbidden_async)
    store = PostgreSQLAuthSessionStore(postgres_session_factory, session_lifetime=timedelta(hours=1))
    logins = [store.create(user_id=store.provision_user(identity=VerifiedExternalIdentity(
        issuer="https://voice.example.test", subject=str(uuid4()),
    ))) for _ in range(2)]
    speaker = Speaker(postgres_engine)
    monkeypatch.setattr(session_routes, "get_database_session_factory", lambda: postgres_session_factory)
    monkeypatch.delitem(app.dependency_overrides, session_routes.get_session_service, raising=False)
    monkeypatch.setitem(app.dependency_overrides, get_auth_session_store, lambda: store)
    monkeypatch.setitem(app.dependency_overrides, get_speech_service, lambda: speaker)
    with TestClient(app, raise_server_exceptions=False, headers={
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={logins[0].credential}",
        AUTH_REQUEST_CONTEXT_HEADER: logins[0].principal.request_context,
    }) as client:
        yield Harness(postgres_session_factory, store, *logins, speaker, client)


@pytest.mark.parametrize("adaptive", [False, True])
def test_current_persisted_question_only_and_no_storage_or_provider_side_effects(harness, adaptive, monkeypatch):
    created = harness.service.start_adaptive() if adaptive else harness.service.start()
    reads = []
    original = InterviewSessionService.get

    def read(service, identifier):
        result = original(service, identifier)
        reads.append((identifier, get_ident()))
        return result

    monkeypatch.setattr(InterviewSessionService, "get", read)
    before = persisted(harness.factory)
    response = harness.speech(created.id)
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert harness.speaker.texts == [created.questions[0]]
    assert len(reads) == 2
    assert all(thread != harness.speaker.thread for _, thread in reads)
    assert persisted(harness.factory) == before


def test_adaptive_generated_question_is_read_exactly_without_generation(harness):
    created = harness.service.start_adaptive()
    harness.service.submit_attempt(created.id, 0, AttemptRequest(
        answer="A saved answer.", expected_last_attempt_number=0,
    ))
    snapshot = harness.service.prepare_continue(created.id, 0, ContinueRequest(expected_last_attempt_number=1))
    text = "What did Café / 咖啡 — e\u0301 teach you?"
    harness.service.commit_continue(snapshot, RoleplayQuestion(
        roleplay_version="live-ai-roleplay-v1", next_question=text,
    ), principal_guard=lambda db: harness.store.revalidate_in_transaction(
        database=db, principal=harness.login.principal,
    ))
    before = persisted(harness.factory)
    assert harness.speech(created.id, 1).status_code == 200
    assert harness.speaker.texts == [text]
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("body", [b'{}', b'{"text":"FORGED_QUESTION"}', b'x' * (3 * 1024 * 1024)],
                         ids=["empty-object", "forged-text", "oversized-body"])
def test_nonempty_body_cannot_substitute_text(harness, body):
    created = harness.service.start()
    before = persisted(harness.factory)
    response = harness.speech(created.id, content=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Speech requests do not accept a body."}
    assert harness.speaker.texts == []
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("selection", ["missing", "foreign", "future", "historical", "completed"])
def test_missing_foreign_noncurrent_and_unpersisted_are_same_fixed_not_found(harness, selection):
    created = harness.service.start_adaptive() if selection == "future" else harness.service.start()
    identifier, index = created.id, 0
    if selection == "missing":
        identifier = uuid4()
    elif selection == "foreign":
        identifier = InterviewSessionService(harness.factory, harness.other_login.principal).start().id
    elif selection == "future":
        index = 1
    else:
        for question in range(5 if selection == "completed" else 1):
            harness.service.submit_attempt(identifier, question, AttemptRequest(
                answer="Saved answer.", expected_last_attempt_number=0,
            ))
            harness.service.continue_question(identifier, question, ContinueRequest(expected_last_attempt_number=1))
    before = persisted(harness.factory)
    response = harness.speech(identifier, index)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert harness.speaker.texts == []
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("error,status", [(SpeechUnavailable, 503), (SpeechFailed, 502), (SpeechTimeout, 504)])
def test_synthesis_failures_are_voice_specific_no_write_no_retry(harness, error, status):
    created = harness.service.start()
    before = persisted(harness.factory)

    def fail():
        raise error()

    harness.speaker.during = fail
    response = harness.speech(created.id)
    assert response.status_code == status
    assert response.json() == UNAVAILABLE
    assert len(harness.speaker.texts) == 1
    assert AUDIO.decode() not in response.text
    assert persisted(harness.factory) == before


@pytest.mark.parametrize("race,status,detail", [
    ("logout", 401, "Authentication required."),
    ("account-switch", 401, "Authentication required."),
    ("context-change", 403, "Invalid authentication request context."),
    ("advance", 404, "Session not found."),
    ("delete", 404, "Session not found."),
])
def test_post_provider_auth_and_question_races_discard_audio(harness, race, status, detail):
    created = harness.service.start()
    if race == "advance":
        harness.service.submit_attempt(created.id, 0, AttemptRequest(
            answer="Saved answer.", expected_last_attempt_number=0,
        ))
    after_change = []

    def during():
        if race in ("logout", "account-switch"):
            harness.store.revoke(auth_session_id=harness.login.principal.auth_session_id)
            if race == "account-switch":
                harness.client.cookies.set(AUTH_SESSION_COOKIE_NAME, harness.other_login.credential)
        elif race == "context-change":
            with harness.factory.begin() as database:
                database.execute(update(AuthSession).where(AuthSession.id == harness.login.principal.auth_session_id)
                                 .values(request_context="replacement-context"))
        elif race == "advance":
            harness.service.continue_question(created.id, 0, ContinueRequest(expected_last_attempt_number=1))
        else:
            with harness.factory.begin() as database:
                database.execute(delete(StoredInterviewSession).where(StoredInterviewSession.id == created.id))
        after_change.append(persisted(harness.factory))

    harness.speaker.during = during
    response = harness.speech(created.id)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert AUDIO not in response.content
    assert len(harness.speaker.texts) == 1
    assert persisted(harness.factory) == after_change[0]


def test_same_question_attempt_changes_do_not_invalidate_speech(harness):
    created = harness.service.start()
    harness.speaker.during = lambda: harness.service.submit_attempt(created.id, 0, AttemptRequest(
        answer="Concurrent answer.", expected_last_attempt_number=0,
    ))
    assert harness.speech(created.id).status_code == 200
    assert len(harness.service.get_attempts(created.id, 0)) == 1


def test_changed_question_projection_discards_audio_without_any_write(harness, monkeypatch):
    created = harness.service.start()
    original = InterviewSessionService.get
    reads = []

    def changed(service, identifier):
        current = original(service, identifier)
        reads.append(identifier)
        if len(reads) == 2:
            return current.model_copy(update={"questions": ["Changed text.", *current.questions[1:]]})
        return current

    monkeypatch.setattr(InterviewSessionService, "get", changed)
    before = persisted(harness.factory)
    response = harness.speech(created.id)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert len(harness.speaker.texts) == 1
    assert persisted(harness.factory) == before


def test_body_rejection_stops_after_first_nonempty_chunk():
    import asyncio
    from fastapi import HTTPException
    from starlette.requests import Request

    received = []

    async def receive():
        received.append(True)
        if len(received) > 1:
            raise AssertionError("Body rejection must not consume the remaining stream.")
        return {"type": "http.request", "body": b"unexpected", "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(session_routes.speak_question(
            session_id=uuid4(), question_index=0, request=request,
            sessions=None, speaker=None, principal=None, auth_store=None,
        ))
    assert error.value.status_code == 422
    assert error.value.detail == "Speech requests do not accept a body."
    assert len(received) == 1


@pytest.mark.parametrize("mode,status,detail", [
    ("missing-cookie", 401, "Authentication required."),
    ("invalid-cookie", 401, "Authentication required."),
    ("wrong-context", 403, "Invalid authentication request context."),
    ("auth-unavailable", 503, "Authentication is temporarily unavailable."),
])
def test_real_auth_contract_blocks_before_provider(harness, monkeypatch, mode, status, detail):
    created = harness.service.start()
    headers = {}
    if mode == "missing-cookie":
        headers["Cookie"] = ""
    elif mode == "invalid-cookie":
        headers["Cookie"] = f"{AUTH_SESSION_COOKIE_NAME}=invalid"
    elif mode == "wrong-context":
        headers[AUTH_REQUEST_CONTEXT_HEADER] = "wrong-context"
    else:
        def unavailable(**kwargs):
            raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")
        monkeypatch.setattr(harness.store, "resolve", unavailable)
    response = harness.speech(created.id, headers=headers)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert response.headers["cache-control"] == "no-store"
    assert harness.speaker.texts == []


@pytest.mark.parametrize("suffix", ["not-a-uuid/questions/0", "00000000-0000-0000-0000-000000000000/questions/-1"])
def test_invalid_paths_never_read_or_synthesize(harness, monkeypatch, suffix):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid path must not read interview storage.")
    monkeypatch.setattr(InterviewSessionService, "get", forbidden)
    assert harness.client.post(f"/api/sessions/{suffix}/speech").status_code == 422
    assert harness.speaker.texts == []

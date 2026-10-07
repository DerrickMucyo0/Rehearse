"""Offline HTTP contracts and isolated PostgreSQL async read boundaries."""

import ast
import asyncio
import inspect
import json
from pathlib import Path
import socket
from threading import get_ident
from typing import get_args
from uuid import UUID

from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app import session_routes as routes
from app.auth import AuthenticatedPrincipal, AuthenticationFailure, AuthenticationFailureKind
from app.auth_http import (
    AUTH_REQUEST_CONTEXT_HEADER, AUTH_SESSION_COOKIE_NAME,
    AuthenticatedPrincipalDependency, get_auth_session_store, require_authenticated_principal,
)
from app.diagnosis import DiagnosisContext
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.nvidia_semantic_diagnosis import (
    NVIDIANemotronSemanticDiagnosisClient,
    NVIDIASemanticDiagnosisFailed,
    NVIDIASemanticDiagnosisTimeout,
    NVIDIASemanticDiagnosisUnavailable,
)
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import (
    SemanticDiagnosisAdapter,
    SemanticDiagnosisAdapterContractError,
)
from app.semantic_diagnosis_application import (
    SemanticDiagnosisFailed,
    SemanticDiagnosisTimeout,
    SemanticDiagnosisUnavailable,
)
from app.semantic_diagnosis_composition import get_semantic_diagnosis_adapter
from app.semantic_diagnosis_json import (
    SemanticDiagnosisJSONContractError,
    parse_semantic_diagnosis_json,
)
from app.sessions import AttemptRequest, InterviewSessionService, SessionNotFound


SESSION_ID = UUID("00000000-0000-4000-8000-000000000013")
ROUTE_PATH = "/api/sessions/{session_id}/questions/{question_index}/attempts/{attempt_number}/diagnosis"
PRIVATE = "NVIDIA PRIVATE-DETAIL API-KEY-MARKER PROVIDER-RESPONSE-MARKER RAW-SEMANTIC-MARKER"


@pytest.fixture(autouse=True)
def offline_boundaries_and_clean_overrides(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    app.dependency_overrides.clear()
    violations = []

    def forbidden(*args, **kwargs):
        violations.append("external operation")
        raise AssertionError("Route tests must not construct providers, access a database, or recalculate metrics.")

    async def forbidden_async(*args, **kwargs):
        violations.append("provider or network request")
        raise AssertionError("Route tests must not make provider or network requests.")

    monkeypatch.setattr(NVIDIANemotronSemanticDiagnosisClient, "__init__", forbidden)
    monkeypatch.setattr(NVIDIANemotronSemanticDiagnosisClient, "request", forbidden_async)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for name in (
        "get_database_session_factory", "measure_transcription", "measure_delivery",
    ):
        monkeypatch.setattr(routes, name, forbidden)
    try:
        yield violations
    finally:
        app.dependency_overrides.clear()
    assert violations == []


def context():
    return DiagnosisContext(
        question="PRIVATE-PERSISTED-QUESTION",
        answer="PRIVATE-PERSISTED-ANSWER",
        question_index=2, attempt_number=3, measurement=None, previous_attempt=None,
    )


def diagnosis():
    return SemanticDiagnosis(
        addressed_question="partially", addressed_question_reason="Synthetic reason.",
        strengths=("Synthetic strength.",), missing_information=("Synthetic missing detail.",),
        structure="mixed", structure_feedback="Synthetic structure feedback.",
        next_focus="specificity", next_focus_reason="Synthetic focus reason.",
        retry_instruction="Synthetic retry instruction.",
    )


class RecordingReader:
    def __init__(self, supplied, events):
        self.context = supplied
        self.events = events
        self.calls = []
        self.threads = []
        self.error = None
        self.active = False
        self.mutations = []
        self.state = {"current_question_index": 4, "status": "active", "latest_attempt": 5}

    def get_diagnosis_context(self, session_id, question_index, attempt_number):
        self.calls.append((session_id, question_index, attempt_number))
        self.threads.append(get_ident())
        self.events.append("reader_begin")
        self.active = True
        try:
            if self.error is not None:
                raise self.error
            return self.context
        finally:
            self.active = False
            self.events.append("reader_end")

    def forbidden_mutation(self, *args, **kwargs):
        self.mutations.append("mutation")
        raise AssertionError("Diagnosis must not write, advance, or read unrelated session state.")

    start = get = get_attempts = get_comparison = forbidden_mutation
    submit_attempt = continue_question = create_measurement = forbidden_mutation
    add = delete = flush = commit = forbidden_mutation


class RecordingAdapter:
    def __init__(self, result, reader, events):
        self.result = result
        self.reader = reader
        self.events = events
        self.calls = []
        self.threads = []
        self.error = None

    async def diagnose(self, supplied):
        assert self.reader.active is False
        self.calls.append(supplied)
        self.threads.append(get_ident())
        self.events.append("adapter_begin")
        if self.error is not None:
            raise self.error
        self.events.append("adapter_end")
        return self.result


class UntouchedAdapter:
    def __init__(self):
        self.accesses = 0

    @property
    def diagnose(self):
        self.accesses += 1
        raise AssertionError("The adapter must not be accessed before a successful persisted read.")


class UnknownFailure(RuntimeError):
    pass


@pytest.fixture
def route_auth(offline_boundaries_and_clean_overrides):
    principal = AuthenticatedPrincipal(
        user_id=UUID("00000000-0000-4000-8000-000000000014"),
        auth_session_id=UUID("00000000-0000-4000-8000-000000000015"),
        request_context="synthetic-semantic-route-context",
    )
    credential = "synthetic-semantic-route-credential"

    class Store:
        def resolve(self, *, credential):
            if credential != "synthetic-semantic-route-credential":
                raise AuthenticationFailure(AuthenticationFailureKind.UNAUTHENTICATED)
            return principal

        def revalidate(self, *, principal):
            assert principal is expected_principal

    expected_principal = principal
    store = Store()
    app.dependency_overrides[get_auth_session_store] = lambda: store
    headers = {
        "Cookie": f"{AUTH_SESSION_COOKIE_NAME}={credential}",
        AUTH_REQUEST_CONTEXT_HEADER: principal.request_context,
    }
    return principal, headers, store


@pytest.fixture
def setup(route_auth):
    expected_principal, headers, _ = route_auth
    supplied, expected, events = context(), diagnosis(), []
    reader = RecordingReader(supplied, events)
    adapter = RecordingAdapter(expected, reader, events)
    dependencies = []

    def session_override(principal: AuthenticatedPrincipalDependency):
        assert principal == expected_principal
        dependencies.append("sessions")
        return reader

    def adapter_override():
        dependencies.append("diagnoser")
        return adapter

    app.dependency_overrides[routes.get_session_service] = session_override
    app.dependency_overrides[get_semantic_diagnosis_adapter] = adapter_override
    with TestClient(app, raise_server_exceptions=True, headers=headers) as client:
        yield client, reader, adapter, expected, events, dependencies


def url(session_id=SESSION_ID, question_index=2, attempt_number=3):
    return ROUTE_PATH.format(
        session_id=session_id, question_index=question_index, attempt_number=attempt_number,
    )


def private_failure(error_type):
    if error_type is SemanticDiagnosisFailed:
        error = error_type("adapter_contract_error")
    elif error_type is NVIDIASemanticDiagnosisFailed:
        error = error_type("provider_transport_error")
    else:
        error = error_type(PRIVATE)
    # Simulate private upstream text without bypassing metadata validation.
    error.args = (PRIVATE,)
    return error


def test_post_matches_shared_versioned_frontend_contract_fixture(setup, capsys, caplog):
    client, reader, adapter, _, events, dependencies = setup
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "frontend/src/fixtures/semanticDiagnosis.v1.json"
    )
    payload = fixture_path.read_text(encoding="utf-8")
    fixture = json.loads(payload)
    adapter.result = parse_semantic_diagnosis_json(payload)

    response = client.post(url())

    assert response.status_code == 200
    assert response.json() == fixture
    assert response.json()["diagnosis_version"] == "semantic-diagnosis-v1"
    assert set(response.json()) == set(SemanticDiagnosis.model_fields)
    assert reader.calls == [(SESSION_ID, 2, 3)] * 2
    assert len(adapter.calls) == 1 and adapter.calls[0] is reader.context
    assert dependencies == ["sessions", "diagnoser"]
    assert events == [
        "reader_begin", "reader_end", "adapter_begin", "adapter_end", "reader_begin", "reader_end",
    ]
    assert reader.mutations == []
    assert "PRIVATE-PERSISTED" not in response.text
    assert "PRIVATE-PERSISTED" not in caplog.text
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("with_unused_input", (False, True), ids=("no-body", "unused-body-and-query"))
def test_post_uses_one_exact_persisted_context_and_only_returns_semantic_diagnosis(
    setup, monkeypatch, with_unused_input, capsys, caplog,
):
    client, reader, adapter, expected, events, dependencies = setup
    supplied = reader.context
    before_context, before_state = supplied.model_dump(), reader.state.copy()
    actual_application = routes.diagnose_application_context
    application_calls = []
    application_results = []

    async def observed(*args, **kwargs):
        application_calls.append((args, kwargs))
        result = await actual_application(*args, **kwargs)
        application_results.append(result)
        return result

    monkeypatch.setattr(routes, "diagnose_application_context", observed)
    extra = {
        "json": {"answer": "SPOOFED-ANSWER", "question": "SPOOFED-QUESTION", "provider": PRIVATE},
        "params": {"answer": "SPOOFED-ANSWER", "question_index": 99, "attempt_number": 99},
    } if with_unused_input else {}
    response = client.post(url(), **extra)
    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
    assert set(response.json()) == set(SemanticDiagnosis.model_fields)
    assert dependencies == ["sessions", "diagnoser"]
    assert len(application_calls) == 1
    args, kwargs = application_calls[0]
    assert len(args) == 2 and args[0] is adapter and args[1] is supplied
    assert kwargs == {}
    assert reader.calls == [(SESSION_ID, 2, 3)] * 2
    assert type(reader.calls[0][0]) is UUID
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert len(application_results) == 1
    assert application_results[0][0] is supplied and application_results[0][1] is expected
    assert len(reader.threads) == 2 and len(adapter.threads) == 1
    assert all(thread != adapter.threads[0] for thread in reader.threads)
    assert events == [
        "reader_begin", "reader_end", "adapter_begin", "adapter_end", "reader_begin", "reader_end",
    ]
    assert reader.mutations == [] and reader.state == before_state
    assert supplied.model_dump() == before_context
    for marker in ("PRIVATE-PERSISTED", "SPOOFED", PRIVATE):
        assert marker not in response.text
        assert marker not in caplog.text
    assert capsys.readouterr() == ("", "")


def test_dependency_factory_override_is_honored_freshly_for_each_post(setup):
    client, reader, _, expected, events, dependencies = setup
    created = []

    def adapter_override():
        adapter = RecordingAdapter(expected, reader, events)
        created.append(adapter)
        return adapter

    app.dependency_overrides[get_semantic_diagnosis_adapter] = adapter_override
    first, second = client.post(url()), client.post(url())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == expected.model_dump(mode="json")
    assert len(created) == 2 and created[0] is not created[1]
    assert all(len(adapter.calls) == 1 and adapter.calls[0] is reader.context for adapter in created)
    assert dependencies == ["sessions", "sessions"]
    assert reader.calls == [(SESSION_ID, 2, 3)] * 4 and reader.mutations == []


def test_missing_attempt_returns_404_without_accessing_adapter_or_application(setup, monkeypatch):
    client, reader, _, _, events, _ = setup
    reader.error = SessionNotFound("Attempt not found.")
    untouched = UntouchedAdapter()
    app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: untouched

    async def forbidden_application(*args, **kwargs):
        raise AssertionError("A missing persisted attempt must not invoke semantic application work.")

    monkeypatch.setattr(routes, "diagnose_application_context", forbidden_application)
    response = client.post(url())
    assert response.status_code == 404
    assert response.json() == {"detail": "Attempt not found."}
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert events == ["reader_begin", "reader_end"]
    assert untouched.accesses == 0 and reader.mutations == []


@pytest.mark.parametrize("error_type,status,detail", (
    (SemanticDiagnosisUnavailable, 503, "Semantic diagnosis is not configured."),
    (SemanticDiagnosisTimeout, 504, "Semantic diagnosis timed out."),
    (SemanticDiagnosisFailed, 502, "Unable to generate semantic diagnosis."),
))
def test_neutral_errors_use_fixed_http_details_despite_private_exception_text(
    setup, error_type, status, detail, capsys, caplog,
):
    client, reader, adapter, _, _, _ = setup
    adapter.error = private_failure(error_type)
    response = client.post(url())
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    for marker in PRIVATE.split():
        assert marker not in response.text + caplog.text
    assert "Traceback" not in response.text
    assert len(reader.calls) == len(adapter.calls) == 1
    assert reader.mutations == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error_type,status,detail", (
    (NVIDIASemanticDiagnosisUnavailable, 503, "Semantic diagnosis is not configured."),
    (NVIDIASemanticDiagnosisTimeout, 504, "Semantic diagnosis timed out."),
    (NVIDIASemanticDiagnosisFailed, 502, "Unable to generate semantic diagnosis."),
    (SemanticDiagnosisJSONContractError, 502, "Unable to generate semantic diagnosis."),
    (SemanticDiagnosisAdapterContractError, 502, "Unable to generate semantic diagnosis."),
))
def test_existing_application_normalizes_low_level_failures_before_http_mapping(
    setup, error_type, status, detail, capsys, caplog,
):
    client, reader, adapter, _, _, _ = setup
    adapter.error = private_failure(error_type)
    response = client.post(url())
    assert response.status_code == status and response.json() == {"detail": detail}
    assert PRIVATE not in response.text + caplog.text
    assert len(reader.calls) == len(adapter.calls) == 1
    assert reader.mutations == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("category,upstream_status,error_type", (
    *(("provider_http_error", status, NVIDIASemanticDiagnosisFailed)
      for status in (401, 403, 404, 422, 429, 500, 502, 503)),
    ("provider_transport_error", None, NVIDIASemanticDiagnosisFailed),
    ("provider_response_contract_error", None, NVIDIASemanticDiagnosisFailed),
    ("semantic_json_contract_error", None, SemanticDiagnosisJSONContractError),
    ("adapter_contract_error", None, SemanticDiagnosisAdapterContractError),
))
def test_safe_in_process_observer_captures_only_normalized_metadata_without_http_exposure(
    setup, monkeypatch, category, upstream_status, error_type, capsys, caplog,
):
    client, reader, adapter, _, events, dependencies = setup
    if error_type is NVIDIASemanticDiagnosisFailed:
        original = error_type(category, upstream_status)
    else:
        original = error_type(PRIVATE)
    original.args = (PRIVATE,)
    adapter.error = original
    before_context, before_state = reader.context.model_dump(), reader.state.copy()
    actual_application = routes.diagnose_application_context
    captured = []

    async def observed(*args, **kwargs):
        try:
            return await actual_application(*args, **kwargs)
        except SemanticDiagnosisFailed as error:
            captured.append({
                "category": error.category,
                "upstream_status": error.upstream_status,
            })
            raise

    monkeypatch.setattr(routes, "diagnose_application_context", observed)
    response = client.post(url())

    assert captured == [{"category": category, "upstream_status": upstream_status}]
    assert type(captured[0]["category"]) is str
    assert captured[0]["upstream_status"] is None or type(captured[0]["upstream_status"]) is int
    assert response.status_code == 502
    assert response.json() == {"detail": "Unable to generate semantic diagnosis."}
    assert "category" not in response.json() and "upstream_status" not in response.json()
    public_output = response.text + json.dumps(dict(response.headers))
    for forbidden in (
        "category", "upstream_status", "provider_http_error", "provider_transport_error",
        "provider_response_contract_error", "semantic_json_contract_error", "adapter_contract_error",
        "PRIVATE-PERSISTED", *PRIVATE.split(),
    ):
        assert forbidden not in public_output + caplog.text
    if upstream_status is not None:
        assert str(upstream_status) not in public_output
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert len(adapter.calls) == 1 and adapter.calls[0] is reader.context
    assert dependencies == ["sessions", "diagnoser"]
    assert events == ["reader_begin", "reader_end", "adapter_begin"]
    assert reader.mutations == [] and reader.state == before_state
    assert reader.context.model_dump() == before_context
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error_type", (ValueError, UnknownFailure))
def test_unknown_failures_propagate_exactly_instead_of_becoming_semantic_http_errors(setup, error_type):
    client, reader, adapter, _, _, _ = setup
    original = error_type("synthetic unknown failure")
    adapter.error = original
    with pytest.raises(error_type) as caught:
        client.post(url())
    assert caught.value is original
    assert len(reader.calls) == len(adapter.calls) == 1 and reader.mutations == []


@pytest.mark.parametrize("error_type", (ValueError, UnknownFailure))
def test_unknown_persisted_reader_failures_propagate_without_application_or_adapter_access(
    setup, monkeypatch, error_type,
):
    client, reader, adapter, _, events, _ = setup
    original = error_type("synthetic persisted reader failure")
    reader.error = original

    async def forbidden_application(*args, **kwargs):
        raise AssertionError("A failed persisted read must not invoke semantic application work.")

    monkeypatch.setattr(routes, "diagnose_application_context", forbidden_application)
    with pytest.raises(error_type) as caught:
        client.post(url())
    assert caught.value is original
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert adapter.calls == [] and reader.mutations == []
    assert events == ["reader_begin", "reader_end"]


def test_real_threadpool_read_finishes_before_application_adapter_and_synthetic_provider_on_event_loop(
    setup, route_auth, monkeypatch,
):
    client, reader, _, expected, events, _ = setup
    actual_threadpool = routes.run_in_threadpool
    actual_application = routes.diagnose_application_context
    route_threads, threadpool_calls, application_calls, adapter_calls, provider_calls = [], [], [], [], []
    route_loops = []

    async def observed_threadpool(function, *args, **kwargs):
        route_threads.append(get_ident())
        route_loops.append(asyncio.get_running_loop())
        threadpool_calls.append((function, args, kwargs))
        events.append(
            "route_before_read" if function == reader.get_diagnosis_context
            else "route_before_auth_revalidation"
        )
        return await actual_threadpool(function, *args, **kwargs)

    async def observed_application(adapter, supplied):
        assert reader.active is False
        assert events == ["route_before_read", "reader_begin", "reader_end"]
        assert get_ident() == route_threads[0]
        assert asyncio.get_running_loop() is route_loops[0]
        application_calls.append((adapter, supplied, get_ident()))
        events.append("application_begin")
        result = await actual_application(adapter, supplied)
        events.append("application_end")
        return result

    class SyntheticProvider:
        async def request(self, supplied):
            assert reader.active is False
            assert get_ident() == route_threads[0]
            assert asyncio.get_running_loop() is route_loops[0]
            provider_calls.append((supplied, get_ident()))
            events.append("provider_begin")
            await asyncio.sleep(0)
            events.append("provider_end")
            return expected

    provider = SyntheticProvider()

    class AsyncAdapter:
        async def diagnose(self, supplied):
            assert reader.active is False
            assert get_ident() == route_threads[0]
            assert asyncio.get_running_loop() is route_loops[0]
            adapter_calls.append((supplied, get_ident()))
            events.append("adapter_begin")
            result = await provider.request(supplied)
            events.append("adapter_end")
            return result

    adapter = AsyncAdapter()
    app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: adapter
    monkeypatch.setattr(routes, "run_in_threadpool", observed_threadpool)
    monkeypatch.setattr(routes, "diagnose_application_context", observed_application)
    response = client.post(url())
    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
    assert len(threadpool_calls) == 3
    function, args, kwargs = threadpool_calls[0]
    assert function == reader.get_diagnosis_context
    assert args == (SESSION_ID, 2, 3) and kwargs == {}
    assert threadpool_calls[1] == (
        routes.revalidate_authenticated_principal, (route_auth[0], route_auth[2]), {},
    )
    assert threadpool_calls[2] == (reader.get_diagnosis_context, args, {})
    assert reader.calls == [args, args]
    assert len(route_threads) == 3 and len(reader.threads) == 2
    assert all(thread == route_threads[0] for thread in route_threads)
    assert all(loop is route_loops[0] for loop in route_loops)
    assert all(thread != route_threads[0] for thread in reader.threads)
    assert application_calls == [(adapter, reader.context, route_threads[0])]
    assert adapter_calls == provider_calls == [(reader.context, route_threads[0])]
    assert application_calls[0][1] is adapter_calls[0][0] is provider_calls[0][0] is reader.context
    assert events == [
        "route_before_read", "reader_begin", "reader_end", "application_begin", "adapter_begin",
        "provider_begin", "provider_end", "adapter_end", "application_end",
        "route_before_auth_revalidation", "route_before_read", "reader_begin", "reader_end",
    ]
    assert reader.mutations == []


def test_http_postgres_transaction_and_connection_end_in_worker_before_semantic_application(
    postgres_engine, postgres_session_factory, authenticated_principal,
    authenticated_session_override, authenticated_http_headers, monkeypatch,
):
    setup_service = InterviewSessionService(postgres_session_factory, authenticated_principal)
    created = setup_service.start()
    expected_answer = "Authoritative persisted answer for the async route boundary."
    submitted = setup_service.submit_attempt(created.id, 0, AttemptRequest(
        answer=expected_answer, expected_last_attempt_number=0,
    ))

    def persisted_rows():
        with postgres_session_factory() as database:
            return {
                model.__tablename__: database.execute(select(model.__table__).order_by(model.id)).all()
                for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
            }

    before = persisted_rows()
    expected = diagnosis()

    class ReadSession(Session):
        pass

    service = InterviewSessionService(
        sessionmaker(bind=postgres_engine, class_=ReadSession), authenticated_principal,
    )
    actual_read = service.get_diagnosis_context
    actual_application = routes.diagnose_application_context
    read_calls, reader_threads, returned, transactions, ended, statements, events = [], [], [], [], [], [], []
    application_calls, adapter_calls, semantic_threads = [], [], []
    active = set()

    def observed_read(session_id, question_index, attempt_number):
        reader_threads.append(get_ident())
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        read_calls.append((session_id, question_index, attempt_number))
        events.append("reader_begin")
        supplied = actual_read(session_id, question_index, attempt_number)
        returned.append(supplied)
        events.append("reader_end")
        return supplied

    def transaction_created(database, transaction):
        transactions.append((database, transaction))
        active.add(transaction)
        events.append("transaction_begin")

    def transaction_ended(database, transaction):
        ended.append((database, transaction))
        active.remove(transaction)
        events.append("transaction_end")

    def observed_statement(connection, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    def forbidden_mutation(*args, **kwargs):
        raise AssertionError("Diagnosis must not write or perform extra public session operations.")

    def assert_read_closed(count=1):
        assert len(read_calls) == len(returned) == len(transactions) == len(ended) == len(statements) == count
        assert active == set()
        for (database, transaction), (ended_database, ended_transaction) in zip(transactions, ended, strict=True):
            assert database is ended_database and transaction is ended_transaction
            assert transaction.is_active is False
            assert database.in_transaction() is False
        assert postgres_engine.pool.checkedout() == 0

    async def observed_application(adapter, supplied):
        assert_read_closed()
        assert events == ["reader_begin", "transaction_begin", "transaction_end", "reader_end"]
        assert asyncio.get_running_loop().is_running()
        semantic_threads.append(get_ident())
        application_calls.append((adapter, supplied))
        assert supplied is returned[0]
        events.append("application_begin")
        return await actual_application(adapter, supplied)

    class TransactionBoundaryAdapter:
        async def diagnose(self, supplied):
            assert_read_closed()
            assert get_ident() == semantic_threads[0]
            assert asyncio.get_running_loop().is_running()
            assert supplied is returned[0]
            assert supplied.answer == expected_answer
            assert supplied.question == created.questions[0]
            assert supplied.question_index == 0 and supplied.attempt_number == submitted.attempt.attempt_number
            adapter_calls.append(supplied)
            events.append("adapter_begin")
            return expected

    adapter = TransactionBoundaryAdapter()
    monkeypatch.setattr(service, "get_diagnosis_context", observed_read)
    for name in ("start", "get", "get_attempts", "get_comparison", "submit_attempt", "continue_question", "create_measurement"):
        monkeypatch.setattr(service, name, forbidden_mutation)
    monkeypatch.setattr(routes, "diagnose_application_context", observed_application)
    app.dependency_overrides[routes.get_session_service] = authenticated_session_override(service)
    app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: adapter
    event.listen(ReadSession, "after_transaction_create", transaction_created)
    event.listen(ReadSession, "after_transaction_end", transaction_ended)
    event.listen(ReadSession, "before_flush", forbidden_mutation)
    event.listen(postgres_engine, "before_cursor_execute", observed_statement)
    try:
        with TestClient(app, raise_server_exceptions=True, headers=authenticated_http_headers) as client:
            response = client.post(url(created.id, 0, submitted.attempt.attempt_number))
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observed_statement)
        event.remove(ReadSession, "before_flush", forbidden_mutation)
        event.remove(ReadSession, "after_transaction_end", transaction_ended)
        event.remove(ReadSession, "after_transaction_create", transaction_created)
    assert response.status_code == 200 and response.json() == expected.model_dump(mode="json")
    assert read_calls == [(created.id, 0, submitted.attempt.attempt_number)] * 2
    assert len(reader_threads) == 2
    assert len(semantic_threads) == len(application_calls) == len(adapter_calls) == 1
    assert all(thread != semantic_threads[0] for thread in reader_threads)
    assert application_calls[0][0] is adapter
    assert application_calls[0][1] is adapter_calls[0] is returned[0]
    assert events == [
        "reader_begin", "transaction_begin", "transaction_end", "reader_end", "application_begin", "adapter_begin",
        "reader_begin", "transaction_begin", "transaction_end", "reader_end",
    ]
    assert_read_closed(2)
    assert all(transaction.parent is None for _, transaction in transactions)
    assert all(statement.lstrip().upper().startswith("SELECT ") for statement in statements)
    assert all("FOR UPDATE" not in statement.upper() for statement in statements)
    assert persisted_rows() == before


@pytest.mark.parametrize("session_id,question_index,attempt_number", (
    ("invalid-uuid", 2, 3), (SESSION_ID, -1, 3), (SESSION_ID, 2, 0),
))
def test_invalid_paths_return_422_without_application_read_adapter_provider_or_database_work(
    setup, monkeypatch, session_id, question_index, attempt_number, offline_boundaries_and_clean_overrides,
):
    client, reader, adapter, _, events, _ = setup
    application_calls = []

    async def forbidden_application(*args, **kwargs):
        application_calls.append((args, kwargs))
        raise AssertionError("Invalid paths must never invoke semantic application work.")

    untouched = UntouchedAdapter()
    app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: untouched
    monkeypatch.setattr(routes, "diagnose_application_context", forbidden_application)
    response = client.post(url(session_id, question_index, attempt_number))
    assert response.status_code == 422
    assert application_calls == [] and reader.calls == [] and adapter.calls == []
    assert untouched.accesses == 0 and events == [] and reader.mutations == []
    assert offline_boundaries_and_clean_overrides == []
    # FastAPI may resolve inert dependencies before validating route paths.
    # Their construction is allowed; application/provider execution is not.


@pytest.mark.parametrize("method", ("GET", "PUT", "PATCH", "DELETE"))
def test_diagnosis_is_not_registered_under_other_http_methods(setup, method):
    client, reader, adapter, _, events, dependencies = setup
    assert client.request(method, url()).status_code == 405
    assert reader.calls == adapter.calls == events == dependencies == []


def test_route_is_post_only_with_semantic_response_no_body_or_query_and_exact_dependency_alias():
    matching = [route for route in iter_route_contexts(app.routes) if route.path == ROUTE_PATH]
    assert len(matching) == 1
    route = matching[0]
    assert isinstance(route.original_route, APIRoute)
    assert route.methods == {"POST"} and route.endpoint is routes.diagnose_attempt
    assert route.response_model is SemanticDiagnosis
    assert route.body_field is None and route.dependant.body_params == []
    assert route.dependant.query_params == []
    assert [field.name for field in route.dependant.path_params] == [
        "session_id", "question_index", "attempt_number",
    ]
    assert [dependency.call for dependency in route.dependant.dependencies] == [
        routes.get_session_service, get_semantic_diagnosis_adapter,
        require_authenticated_principal, get_auth_session_store,
    ]
    adapter_type, dependency = get_args(routes.SemanticDiagnosisService)
    assert adapter_type is SemanticDiagnosisAdapter
    assert dependency.dependency is get_semantic_diagnosis_adapter
    assert inspect.iscoroutinefunction(routes.diagnose_attempt)
    signature = inspect.signature(routes.diagnose_attempt)
    assert tuple(signature.parameters) == (
        "session_id", "question_index", "attempt_number", "sessions", "diagnoser", "principal", "auth_store",
    )
    assert all(parameter.default is inspect.Parameter.empty for parameter in signature.parameters.values())
    assert signature.return_annotation is SemanticDiagnosis


def test_route_source_is_provider_neutral_and_delegates_once_without_lower_level_calls_or_writes():
    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module: {name.name for name in node.names}
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module.startswith("app.semantic_diagnosis")
    }
    assert imported == {
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
        "app.semantic_diagnosis_adapter": {"SemanticDiagnosisAdapter"},
        "app.semantic_diagnosis_application": {
            "SemanticDiagnosisUnavailable", "SemanticDiagnosisTimeout", "SemanticDiagnosisFailed",
            "diagnose_application_context",
        },
        "app.semantic_diagnosis_composition": {"get_semantic_diagnosis_adapter"},
    }
    for forbidden in (
        "app.nvidia_semantic_diagnosis", "app.semantic_diagnosis_json", "app.semantic_diagnosis_client",
        "app.semantic_diagnosis_prompt", "app.semantic_diagnosis_eval", "NVIDIA", "nemotron-",
        "integrate.api.nvidia.com", "SemanticDiagnosisJSONContractError", "JSONSemanticDiagnosisAdapter",
        "parse_semantic_diagnosis_json", "build_semantic_diagnosis_prompt",
    ):
        assert forbidden not in source
    endpoint = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "diagnose_attempt")
    body_nodes = [node for statement in endpoint.body for node in ast.walk(statement)]
    assert not any(isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)) for node in body_nodes)
    attributes = [node for node in body_nodes if isinstance(node, ast.Attribute)]
    assert len(attributes) == 2
    expected_read = ast.Attribute(
        value=ast.Name(id="sessions", ctx=ast.Load()), attr="get_diagnosis_context", ctx=ast.Load(),
    )
    assert all(ast.dump(attribute) == ast.dump(expected_read) for attribute in attributes)
    calls = [node for node in body_nodes if isinstance(node, ast.Call)]
    assert all(isinstance(node.func, ast.Name) for node in calls)
    assert {node.func.id for node in calls} == {
        "run_in_threadpool", "diagnose_application_context", "HTTPException", "str",
    }
    threadpool_calls = [node for node in calls if node.func.id == "run_in_threadpool"]
    assert len(threadpool_calls) == 3
    expected_read_call = ast.Call(
        func=ast.Name(id="run_in_threadpool", ctx=ast.Load()),
        args=[expected_read,
              *[ast.Name(id=name, ctx=ast.Load()) for name in ("session_id", "question_index", "attempt_number")]],
        keywords=[],
    )
    assert ast.dump(threadpool_calls[0]) == ast.dump(expected_read_call)
    assert ast.dump(threadpool_calls[2]) == ast.dump(expected_read_call)
    assert ast.dump(threadpool_calls[1]) == ast.dump(ast.Call(
        func=ast.Name(id="run_in_threadpool", ctx=ast.Load()),
        args=[ast.Name(id=name, ctx=ast.Load()) for name in (
            "revalidate_authenticated_principal", "principal", "auth_store",
        )],
        keywords=[],
    ))
    application_calls = [node for node in calls if node.func.id == "diagnose_application_context"]
    assert len(application_calls) == 1
    assert ast.dump(application_calls[0]) == ast.dump(ast.Call(
        func=ast.Name(id="diagnose_application_context", ctx=ast.Load()),
        args=[ast.Name(id="diagnoser", ctx=ast.Load()), ast.Name(id="context", ctx=ast.Load())],
        keywords=[],
    ))
    awaits = [node for node in body_nodes if isinstance(node, ast.Await)]
    assert len(awaits) == 4
    assert awaits[0].value is threadpool_calls[0] and awaits[1].value is application_calls[0]
    assert awaits[2].value is threadpool_calls[1] and awaits[3].value is threadpool_calls[2]
    operations = [node for node in endpoint.body if isinstance(node, ast.Try)]
    assert len(operations) == 3
    assert all(len(operation.body) == 1 for operation in operations)
    assert isinstance(operations[0].body[0], ast.Assign)
    assert operations[0].body[0].value is awaits[0]
    assert isinstance(operations[1].body[0], ast.Assign)
    assert operations[1].body[0].value is awaits[1]
    assert isinstance(operations[2].body[0], ast.Assign)
    assert operations[2].body[0].value is awaits[3]
    assert [handler.type.id for handler in operations[0].handlers] == ["SessionNotFound"]
    assert [handler.type.id for handler in operations[1].handlers] == [
        "SemanticDiagnosisUnavailable", "SemanticDiagnosisTimeout", "SemanticDiagnosisFailed",
    ]
    assert [handler.type.id for handler in operations[2].handlers] == ["SessionNotFound"]
    handlers = [node for node in body_nodes if isinstance(node, ast.ExceptHandler)]
    assert all(isinstance(node.type, ast.Name) for node in handlers)
    assert [node.type.id for node in handlers] == [
        "SessionNotFound", "SemanticDiagnosisUnavailable", "SemanticDiagnosisTimeout", "SemanticDiagnosisFailed",
        "SessionNotFound",
    ]
    returns = [node for node in body_nodes if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Name)
    assert returns[0].value.id == "diagnosis"

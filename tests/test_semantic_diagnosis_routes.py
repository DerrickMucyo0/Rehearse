"""Offline HTTP contracts using synthetic persisted readers and semantic adapters."""

import ast
import inspect
from pathlib import Path
import socket
from typing import get_args
from uuid import UUID

from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import app
from app import session_routes as routes
from app.diagnosis import DiagnosisContext
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
from app.semantic_diagnosis_json import SemanticDiagnosisJSONContractError
from app.sessions import SessionNotFound


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
        "create_database_engine", "create_session_factory", "measure_transcription", "measure_delivery",
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
        self.error = None
        self.active = False
        self.mutations = []
        self.state = {"current_question_index": 4, "status": "active", "latest_attempt": 5}

    def get_diagnosis_context(self, session_id, question_index, attempt_number):
        self.calls.append((session_id, question_index, attempt_number))
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
        raise AssertionError("Diagnosis must not write, advance, or independently reread a session.")

    start = get = get_attempts = get_comparison = forbidden_mutation
    submit_attempt = continue_question = create_measurement = forbidden_mutation
    add = delete = flush = commit = forbidden_mutation


class RecordingAdapter:
    def __init__(self, result, reader, events):
        self.result = result
        self.reader = reader
        self.events = events
        self.calls = []
        self.error = None

    async def diagnose(self, supplied):
        assert self.reader.active is False
        self.calls.append(supplied)
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
def setup():
    supplied, expected, events = context(), diagnosis(), []
    reader = RecordingReader(supplied, events)
    adapter = RecordingAdapter(expected, reader, events)
    dependencies = []

    def session_override():
        dependencies.append("sessions")
        return reader

    def adapter_override():
        dependencies.append("diagnoser")
        return adapter

    app.dependency_overrides[routes.get_session_service] = session_override
    app.dependency_overrides[get_semantic_diagnosis_adapter] = adapter_override
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, reader, adapter, expected, events, dependencies


def url(session_id=SESSION_ID, question_index=2, attempt_number=3):
    return ROUTE_PATH.format(
        session_id=session_id, question_index=question_index, attempt_number=attempt_number,
    )


@pytest.mark.parametrize("with_unused_input", (False, True), ids=("no-body", "unused-body-and-query"))
def test_post_uses_one_exact_persisted_context_and_only_returns_semantic_diagnosis(
    setup, monkeypatch, with_unused_input, capsys, caplog,
):
    client, reader, adapter, expected, events, dependencies = setup
    supplied = reader.context
    before_context, before_state = supplied.model_dump(), reader.state.copy()
    actual_application = routes.diagnose_application_attempt
    application_calls = []
    application_results = []

    async def observed(*args, **kwargs):
        application_calls.append((args, kwargs))
        result = await actual_application(*args, **kwargs)
        application_results.append(result)
        return result

    monkeypatch.setattr(routes, "diagnose_application_attempt", observed)
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
    assert len(args) == 2 and args[0] is reader and args[1] is adapter
    assert kwargs == {"session_id": SESSION_ID, "question_index": 2, "attempt_number": 3}
    assert type(kwargs["session_id"]) is UUID
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert reader.calls[0][0] is kwargs["session_id"]
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert len(application_results) == 1
    assert application_results[0][0] is supplied and application_results[0][1] is expected
    assert events == ["reader_begin", "reader_end", "adapter_begin", "adapter_end"]
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
    assert len(reader.calls) == 2 and reader.mutations == []


def test_missing_attempt_returns_404_without_accessing_adapter(setup):
    client, reader, _, _, events, _ = setup
    reader.error = SessionNotFound("Attempt not found.")
    untouched = UntouchedAdapter()
    app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: untouched
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
    adapter.error = error_type(PRIVATE)
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
    adapter.error = error_type(PRIVATE)
    response = client.post(url())
    assert response.status_code == status and response.json() == {"detail": detail}
    assert PRIVATE not in response.text + caplog.text
    assert len(reader.calls) == len(adapter.calls) == 1
    assert reader.mutations == []
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
    monkeypatch.setattr(routes, "diagnose_application_attempt", forbidden_application)
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
    ]
    adapter_type, dependency = get_args(routes.SemanticDiagnosisService)
    assert adapter_type is SemanticDiagnosisAdapter
    assert dependency.dependency is get_semantic_diagnosis_adapter
    assert inspect.iscoroutinefunction(routes.diagnose_attempt)
    signature = inspect.signature(routes.diagnose_attempt)
    assert tuple(signature.parameters) == (
        "session_id", "question_index", "attempt_number", "sessions", "diagnoser",
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
            "diagnose_application_attempt",
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
    assert not any(isinstance(node, (ast.Attribute, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)) for node in body_nodes)
    calls = [node for node in body_nodes if isinstance(node, ast.Call)]
    assert all(isinstance(node.func, ast.Name) for node in calls)
    assert {node.func.id for node in calls} == {"diagnose_application_attempt", "HTTPException", "str"}
    application_calls = [node for node in calls if node.func.id == "diagnose_application_attempt"]
    assert len(application_calls) == 1
    assert ast.dump(application_calls[0]) == ast.dump(ast.Call(
        func=ast.Name(id="diagnose_application_attempt", ctx=ast.Load()),
        args=[ast.Name(id="sessions", ctx=ast.Load()), ast.Name(id="diagnoser", ctx=ast.Load())],
        keywords=[ast.keyword(arg=name, value=ast.Name(id=name, ctx=ast.Load())) for name in (
            "session_id", "question_index", "attempt_number",
        )],
    ))
    awaits = [node for node in body_nodes if isinstance(node, ast.Await)]
    assert len(awaits) == 1 and awaits[0].value is application_calls[0]
    handlers = [node for node in body_nodes if isinstance(node, ast.ExceptHandler)]
    assert all(isinstance(node.type, ast.Name) for node in handlers)
    assert [node.type.id for node in handlers] == [
        "SessionNotFound", "SemanticDiagnosisUnavailable", "SemanticDiagnosisTimeout", "SemanticDiagnosisFailed",
    ]
    returns = [node for node in body_nodes if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Name)
    assert returns[0].value.id == "diagnosis"

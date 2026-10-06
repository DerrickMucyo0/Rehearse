"""Offline transport contracts; all answers, credentials, and responses are synthetic."""

import ast
import asyncio
import inspect
import json
from pathlib import Path
import re
import traceback
from types import SimpleNamespace
from typing import get_type_hints
from uuid import UUID

import httpx
import pytest

from app import nvidia_semantic_diagnosis as provider
from app import semantic_diagnosis_client as client_boundary
from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import diagnose_persisted_attempt
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_client import (
    JSONSemanticDiagnosisAdapter, SemanticDiagnosisJSONClient,
)
from app.semantic_diagnosis_json import SemanticDiagnosisJSONContractError
from app.semantic_diagnosis_prompt import SemanticDiagnosisPrompt


FAKE_KEY = "synthetic-test-key-never-a-real-credential"
PRIVATE_BODY = "SYNTHETIC_PRIVATE_RESPONSE_BODY_84271"
PRIVATE_HEADER = "SYNTHETIC_PRIVATE_REQUEST_ID_29643"
PRIVATE_METADATA = "SYNTHETIC_PRIVATE_PROVIDER_METADATA_73591"
PRIVATE_NETWORK = "SYNTHETIC_PRIVATE_TRANSPORT_DETAIL_61847"
PRIVATE_CONTENT = "SYNTHETIC_PRIVATE_ASSISTANT_CONTENT_95326"
PRIVATE_QUESTION = "SYNTHETIC_PRIVATE_QUESTION_42785"
FAILURE_MESSAGE = "Semantic diagnosis provider request failed."
TIMEOUT_MESSAGE = "Semantic diagnosis provider timed out."
UNAVAILABLE_MESSAGE = "Semantic diagnosis provider is not configured."
Client = provider.NVIDIANemotronSemanticDiagnosisClient


@pytest.fixture(autouse=True)
def only_fake_credentials_and_mock_transport(monkeypatch):
    # No operating-system credential is configured; only the client sees this
    # synthetic in-memory configuration, and all HTTP is mocked.
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ={"NVIDIA_API_KEY": FAKE_KEY}))

    async def forbidden_async_network(*args, **kwargs):
        raise AssertionError("Real HTTP transport must never run in these tests.")

    def forbidden_sync_network(*args, **kwargs):
        raise AssertionError("Real HTTP transport must never run in these tests.")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async_network,
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync_network)


@pytest.fixture
def context():
    return DiagnosisContext(
        question=f" \t{PRIVATE_QUESTION}: describe a synthetic situation. 中文\n ",
        answer=" \nI arranged synthetic cards, then counted them. 😀\t ",
        question_index=4,
        attempt_number=2,
        measurement=None,
        previous_attempt=None,
    )


@pytest.fixture
def diagnosis():
    return SemanticDiagnosis(
        addressed_question="partially",
        addressed_question_reason="The synthetic answer describes an action.",
        strengths=("The synthetic action is concrete.",),
        missing_information=("The synthetic result is absent.",),
        structure="mixed",
        structure_feedback="The synthetic action follows the situation.",
        next_focus="completeness",
        next_focus_reason="A synthetic result would complete the example.",
        retry_instruction="State the synthetic result in one sentence.",
    )


def envelope(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


def run_request(transport, context):
    return asyncio.run(Client(transport=transport).request(context))


def assert_private_failure(
    error, expected_type, message, capsys, caplog,
    *, category="provider_response_contract_error", upstream_status=None,
):
    assert type(error) is expected_type
    assert isinstance(error, RuntimeError)
    assert error.args == (message,)
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__dict__ == {}
    if expected_type is provider.NVIDIASemanticDiagnosisFailed:
        assert error.category == category
        assert type(error.category) is str
        assert error.upstream_status == upstream_status
        assert error.upstream_status is None or type(error.upstream_status) is int
    else:
        assert not hasattr(error, "category")
        assert not hasattr(error, "upstream_status")
    public = "\n".join((str(error), repr(error), "".join(traceback.format_exception(error))))
    for marker in (
        FAKE_KEY, PRIVATE_BODY, PRIVATE_HEADER, PRIVATE_METADATA,
        PRIVATE_NETWORK, PRIVATE_CONTENT, PRIVATE_QUESTION,
    ):
        assert marker not in public
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_public_constants_and_structural_signature():
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_ENDPOINT == (
        "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_MODEL == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_CONNECT_TIMEOUT_SECONDS == 60
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_READ_TIMEOUT_SECONDS is None
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_WRITE_TIMEOUT_SECONDS == 60
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_POOL_TIMEOUT_SECONDS == 60
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_TOTAL_TIMEOUT_SECONDS == 120
    assert provider.NVIDIA_SEMANTIC_DIAGNOSIS_MAX_TOKENS == 8192
    assert Client.__bases__ == (object,)
    constructor = inspect.signature(Client.__init__)
    assert tuple(constructor.parameters) == ("self", "transport")
    assert constructor.parameters["transport"].default is None
    assert get_type_hints(Client.__init__) == {
        "transport": httpx.AsyncBaseTransport | None, "return": type(None),
    }
    assert inspect.iscoroutinefunction(Client.request)
    assert inspect.signature(Client.request) == inspect.signature(SemanticDiagnosisJSONClient.request)
    assert get_type_hints(Client.request) == {"context": DiagnosisContext, "return": str}
    for error_type in (
        provider.NVIDIASemanticDiagnosisUnavailable,
        provider.NVIDIASemanticDiagnosisTimeout,
        provider.NVIDIASemanticDiagnosisFailed,
    ):
        assert error_type.__bases__ == (RuntimeError,)


def test_frontend_diagnosis_timeout_exceeds_backend_total_provider_deadline():
    source = (
        Path(__file__).resolve().parents[1] / "frontend" / "src" / "interviewApi.ts"
    ).read_text()
    match = re.search(
        r"^const SEMANTIC_DIAGNOSIS_TIMEOUT_MS = ([\d_]+)$", source, re.MULTILINE,
    )
    assert match is not None
    frontend_timeout_ms = int(match.group(1).replace("_", ""))
    assert frontend_timeout_ms == 135_000
    assert frontend_timeout_ms > provider.NVIDIA_SEMANTIC_DIAGNOSIS_TOTAL_TIMEOUT_SECONDS * 1000
    assert "AbortSignal.timeout(SEMANTIC_DIAGNOSIS_TIMEOUT_MS)" in source


@pytest.mark.parametrize("configured_key", [None, "", " ", "\t\r\n "])
def test_missing_configuration_never_builds_prompt_or_opens_http(
    configured_key, context, monkeypatch, capsys, caplog,
):
    if configured_key is None:
        monkeypatch.delitem(provider.os.environ, "NVIDIA_API_KEY")
    else:
        monkeypatch.setitem(provider.os.environ, "NVIDIA_API_KEY", configured_key)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Missing configuration must stop before prompt and HTTP.")

    transport = httpx.MockTransport(forbidden)
    client = Client(transport=transport)
    monkeypatch.setattr(provider, "build_semantic_diagnosis_prompt", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(provider.NVIDIASemanticDiagnosisUnavailable) as caught:
        asyncio.run(client.request(context))
    assert calls == []
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisUnavailable,
        UNAVAILABLE_MESSAGE, capsys, caplog,
    )


def test_key_is_loaded_at_request_time_and_trimmed(context, monkeypatch):
    monkeypatch.delitem(provider.os.environ, "NVIDIA_API_KEY")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=envelope("synthetic raw result"))

    client = Client(transport=httpx.MockTransport(respond))
    monkeypatch.setitem(provider.os.environ, "NVIDIA_API_KEY", f" \t{FAKE_KEY}\r\n ")
    assert asyncio.run(client.request(context)) == "synthetic raw result"
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_prompt_identity_exact_http_contract_and_no_context_inspection(context, monkeypatch):
    prompt = SemanticDiagnosisPrompt(
        system=" \tSYNTHETIC_SYSTEM_INSTRUCTIONS 中文\n ",
        user=" \r\nSYNTHETIC_RAW_USER_PAYLOAD 😀\t ",
    )
    events = []
    requests = []
    constructions = []
    actual_http_client = httpx.AsyncClient

    def build(supplied_context):
        assert supplied_context is context
        events.append("prompt")
        return prompt

    def respond(request):
        events.append("http")
        requests.append(request)
        return httpx.Response(200, json=envelope("synthetic raw content"))

    transport = httpx.MockTransport(respond)

    def construct(*args, **kwargs):
        constructions.append((args, kwargs))
        return actual_http_client(*args, **kwargs)

    actual_getattribute = DiagnosisContext.__getattribute__

    def guarded_getattribute(self, name):
        if name in DiagnosisContext.model_fields:
            raise AssertionError("The provider must delegate context ownership to the prompt builder.")
        return actual_getattribute(self, name)

    def forbidden_transform(*args, **kwargs):
        raise AssertionError("The provider must not transform or serialize the context.")

    monkeypatch.setattr(provider, "build_semantic_diagnosis_prompt", build)
    monkeypatch.setattr(httpx, "AsyncClient", construct)
    monkeypatch.setattr(DiagnosisContext, "__getattribute__", guarded_getattribute)
    for method in (
        "__init__", "model_validate", "model_validate_json", "model_construct",
        "model_dump", "model_dump_json", "model_copy",
    ):
        monkeypatch.setattr(DiagnosisContext, method, forbidden_transform)

    assert run_request(transport, context) == "synthetic raw content"
    assert events == ["prompt", "http"]
    assert len(constructions) == 1
    args, kwargs = constructions[0]
    assert args == ()
    assert kwargs == {
        "transport": transport,
        "timeout": httpx.Timeout(connect=60, read=None, write=60, pool=60),
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == provider.NVIDIA_SEMANTIC_DIAGNOSIS_ENDPOINT
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    assert request.extensions["timeout"] == {
        "connect": 60, "read": None, "write": 60, "pool": 60,
    }
    body = json.loads(request.content)
    assert body == {
        "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "temperature": 0.0,
        "max_tokens": 8192,
        "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert body["stream"] is False
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert FAKE_KEY.encode() not in request.content


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_all_success_statuses_return_content_and_ignore_unrelated_metadata(
    status, context, capsys, caplog,
):
    raw = " \tSYNTHETIC_RAW_CONTENT 中文😀\r\n "
    reply = envelope(raw)
    reply.update({
        "id": PRIVATE_HEADER,
        "created": PRIVATE_METADATA,
        "model": "a synthetic metadata model different from the requested model",
        "usage": {"unexpected_shape": PRIVATE_METADATA},
        "service_tier": PRIVATE_METADATA,
        "extra": {"private": PRIVATE_BODY},
    })
    reply["choices"][0].update({"index": 97, "reasoning": PRIVATE_METADATA})
    reply["choices"][0]["message"]["reasoning_content"] = PRIVATE_METADATA
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=reply, headers={"x-request-id": PRIVATE_HEADER})

    assert run_request(httpx.MockTransport(respond), context) == raw
    assert len(requests) == 1
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_raw_content_identity_is_preserved_without_mutating_envelope(context, monkeypatch):
    raw = "".join([" \t", PRIVATE_CONTENT, " 中文\r\n "])
    reply = envelope(raw)
    response = httpx.Response(200)
    calls = []

    def decode():
        calls.append("json")
        return reply

    monkeypatch.setattr(response, "json", decode)
    transport = httpx.MockTransport(lambda request: response)
    assert run_request(transport, context) is raw
    assert calls == ["json"]
    assert reply == envelope(raw)
    assert reply["choices"][0]["message"]["content"] is raw


@pytest.mark.parametrize("raw", [
    " ", "\t\r\n ", "```json\n{bad}\n```", "{broken synthetic json",
    "synthetic unstructured prose", "{\"addressed_question\": \"not_an_enum\"}",
])
def test_raw_transport_does_not_parse_repair_trim_or_reject_semantic_content(raw, context):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(raw))

    assert run_request(httpx.MockTransport(respond), context) == raw
    assert len(requests) == 1


_MISSING = object()


@pytest.mark.parametrize("finish_reason", [
    _MISSING, None, "", "length", "content_filter", "tool_calls",
    "synthetic_unknown_reason", "STOP", "stop ", 1, True, [], {},
], ids=[
    "missing", "null", "empty", "length", "content-filter", "tool-calls",
    "unknown", "uppercase", "padded", "integer", "boolean", "list", "dict",
])
def test_incomplete_or_invalid_finish_reason_fails_even_with_valid_semantic_json(
    finish_reason, context, diagnosis, capsys, caplog,
):
    reply = envelope(diagnosis.model_dump_json())
    reply["private"] = PRIVATE_BODY
    choice = reply["choices"][0]
    if finish_reason is _MISSING:
        del choice["finish_reason"]
    else:
        choice["finish_reason"] = finish_reason
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=reply, headers={"x-request-id": PRIVATE_HEADER})

    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(respond), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
    )


def malformed_envelopes():
    good_choice = {"finish_reason": "stop", "message": {"content": PRIVATE_CONTENT}}
    return [
        ("null", None), ("array", []), ("string", PRIVATE_BODY),
        ("empty-object", {}), ("missing-choices", {"private": PRIVATE_BODY}),
        ("null-choices", {"choices": None}), ("object-choices", {"choices": {}}),
        ("empty-choices", {"choices": []}),
        ("multiple-choices", {"choices": [good_choice, good_choice]}),
        ("null-choice", {"choices": [None]}),
        ("string-choice", {"choices": [PRIVATE_BODY]}),
        ("missing-message", {"choices": [{"finish_reason": "stop"}]}),
        ("null-message", {"choices": [{"finish_reason": "stop", "message": None}]}),
        ("string-message", {"choices": [{"finish_reason": "stop", "message": PRIVATE_BODY}]}),
        ("missing-content", {"choices": [{"finish_reason": "stop", "message": {}}]}),
        *[(f"{name}-content", envelope(content)) for name, content in [
            ("null", None), ("integer", 83729), ("boolean", True),
            ("list", [PRIVATE_CONTENT]), ("object", {"private": PRIVATE_CONTENT}),
            ("empty", ""),
        ]],
    ]


@pytest.mark.parametrize("name,reply", malformed_envelopes(), ids=lambda value: value if isinstance(value, str) else None)
def test_invalid_success_envelope_fails_without_retry_or_details(
    name, reply, context, capsys, caplog,
):
    requests = []

    def respond(request):
        requests.append(request)
        # httpx treats json=None as no body; serialize here to exercise JSON null.
        return httpx.Response(
            200, content=json.dumps(reply, ensure_ascii=False),
            headers={"Content-Type": "application/json", "x-request-id": PRIVATE_HEADER},
        )

    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(respond), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
    )


def test_invalid_response_json_discards_body_and_decode_error(context, capsys, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, content=f"{PRIVATE_BODY}: not JSON", headers={"x-request-id": PRIVATE_HEADER},
        )

    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(respond), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
    )


def test_json_value_error_is_not_retained_as_context(context, monkeypatch, capsys, caplog):
    response = httpx.Response(200, headers={"x-request-id": PRIVATE_HEADER})
    calls = []

    def decode():
        calls.append("json")
        raise ValueError(PRIVATE_NETWORK)

    monkeypatch.setattr(response, "json", decode)
    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(lambda request: response), context)
    assert calls == ["json"]
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
    )


@pytest.mark.parametrize("status", [
    100, 199, 300, 301, 307, 400, 401, 403, 404, 408, 409, 422, 429,
    500, 502, 503, 504, 599,
])
def test_non_success_http_status_never_decodes_body_or_retries(
    status, context, monkeypatch, capsys, caplog,
):
    response = httpx.Response(
        status, json={"private": PRIVATE_BODY}, headers={"x-request-id": PRIVATE_HEADER},
    )
    requests = []

    def forbidden_decode():
        raise AssertionError("HTTP failure must be checked before response JSON.")

    def respond(request):
        requests.append(request)
        return response

    monkeypatch.setattr(response, "json", forbidden_decode)
    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(respond), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
        category="provider_http_error", upstream_status=status,
    )


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500, 503])
def test_http_failure_classifier_does_not_access_body_headers_or_response_metadata(
    status, monkeypatch, capsys, caplog,
):
    response = httpx.Response(status, content=PRIVATE_BODY)

    def forbidden(*args, **kwargs):
        raise AssertionError("The failure classifier must inspect only HTTP status.")

    for name in ("json", "read", "aread", "iter_bytes", "iter_raw", "aiter_bytes", "aiter_raw"):
        monkeypatch.setattr(response, name, forbidden)
    monkeypatch.setattr(httpx.Response, "content", property(forbidden))
    monkeypatch.setattr(httpx.Response, "text", property(forbidden))
    response.headers = object()

    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        provider._assistant_content(response)

    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
        category="provider_http_error", upstream_status=status,
    )


@pytest.mark.parametrize(("category", "status"), [
    ("provider_http_error", None), ("provider_http_error", 401),
    ("provider_transport_error", None), ("provider_response_contract_error", None),
])
def test_provider_failure_metadata_contains_only_allowlisted_primitives(category, status):
    error = provider.NVIDIASemanticDiagnosisFailed(category, status)
    assert error.category == category and type(error.category) is str
    assert error.upstream_status == status
    assert error.__dict__ == {}
    assert error.args == (FAILURE_MESSAGE,)
    assert str(error) == FAILURE_MESSAGE
    assert repr(error) == f"NVIDIASemanticDiagnosisFailed({FAILURE_MESSAGE!r})"


@pytest.mark.parametrize(("category", "status"), [
    (PRIVATE_METADATA, None), (None, None), (1, None), ([], None), ({}, None),
    ("semantic_json_contract_error", None), ("adapter_contract_error", None),
    ("provider_http_error", True), ("provider_http_error", 401.0),
    ("provider_http_error", PRIVATE_METADATA), ("provider_http_error", {}),
    ("provider_transport_error", 401), ("provider_response_contract_error", 200),
])
def test_provider_failure_metadata_rejects_unallowlisted_values_without_formatting(category, status):
    with pytest.raises(ValueError) as caught:
        provider.NVIDIASemanticDiagnosisFailed(category, status)
    assert str(caught.value) in {
        "Invalid semantic diagnosis failure metadata.",
        "Invalid semantic diagnosis provider failure category.",
    }
    assert PRIVATE_METADATA not in str(caught.value) + repr(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("error_type", [
    httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
])
def test_network_errors_are_private_and_never_retried(error_type, context, capsys, caplog):
    requests = []

    def fail(request):
        requests.append(request)
        raise error_type(PRIVATE_NETWORK, request=request)

    with pytest.raises(provider.NVIDIASemanticDiagnosisFailed) as caught:
        run_request(httpx.MockTransport(fail), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisFailed, FAILURE_MESSAGE, capsys, caplog,
        category="provider_transport_error",
    )


@pytest.mark.parametrize("error_type", [
    httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout,
    httpx.WriteTimeout, httpx.PoolTimeout, TimeoutError,
])
def test_timeout_errors_are_private_and_never_retried(error_type, context, capsys, caplog):
    requests = []

    def fail(request):
        requests.append(request)
        if error_type is TimeoutError:
            raise error_type(PRIVATE_NETWORK)
        raise error_type(PRIVATE_NETWORK, request=request)

    with pytest.raises(provider.NVIDIASemanticDiagnosisTimeout) as caught:
        run_request(httpx.MockTransport(fail), context)
    assert len(requests) == 1
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisTimeout, TIMEOUT_MESSAGE, capsys, caplog,
    )


def test_total_deadline_cancels_single_http_operation_and_normalizes_privately(
    context, monkeypatch, capsys, caplog,
):
    deadlines, requests, cancellations = [], [], []
    real_timeout = asyncio.timeout

    def immediate_deadline(seconds):
        deadlines.append(seconds)
        # Exercise the real timeout mechanism on the next event-loop turn.
        return real_timeout(0)

    async def stalled(request):
        requests.append(request)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append(True)
            raise
        raise AssertionError("A cancelled operation must not finish.")

    monkeypatch.setattr(provider, "asyncio", SimpleNamespace(timeout=immediate_deadline))
    with pytest.raises(provider.NVIDIASemanticDiagnosisTimeout) as caught:
        run_request(httpx.MockTransport(stalled), context)
    assert deadlines == [120]
    assert len(requests) == 1 and cancellations == [True]
    assert_private_failure(
        caught.value, provider.NVIDIASemanticDiagnosisTimeout, TIMEOUT_MESSAGE, capsys, caplog,
    )


def test_total_deadline_preserves_application_http_504_and_persisted_context(
    context, monkeypatch, capsys, caplog,
):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.session_routes import get_transitional_provider_session_service
    from app.semantic_diagnosis_composition import get_semantic_diagnosis_adapter

    deadlines, requests, reads = [], [], []
    real_timeout = asyncio.timeout
    before = context.model_dump()

    def immediate_deadline(seconds):
        deadlines.append(seconds)
        return real_timeout(0)

    async def stalled(request):
        requests.append(request)
        await asyncio.Event().wait()
        raise AssertionError("A cancelled operation must not finish.")

    class Reader:
        def get_diagnosis_context(self, session_id, question_index, attempt_number):
            reads.append((session_id, question_index, attempt_number))
            return context

    reader = Reader()
    adapter = JSONSemanticDiagnosisAdapter(Client(transport=httpx.MockTransport(stalled)))
    monkeypatch.setattr(provider, "asyncio", SimpleNamespace(timeout=immediate_deadline))
    original_overrides = app.dependency_overrides.copy()
    try:
        app.dependency_overrides[get_transitional_provider_session_service] = lambda: reader
        app.dependency_overrides[get_semantic_diagnosis_adapter] = lambda: adapter
        with TestClient(app) as client:
            response = client.post(
                "/api/sessions/00000000-0000-4000-8000-000000000017/questions/4/attempts/2/diagnosis"
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
    assert response.status_code == 504
    assert response.json() == {"detail": "Semantic diagnosis timed out."}
    assert deadlines == [120] and len(requests) == len(reads) == 1
    assert reads == [(UUID("00000000-0000-4000-8000-000000000017"), 4, 2)]
    assert context.model_dump() == before
    public = response.text + json.dumps(dict(response.headers)) + caplog.text
    for marker in (
        FAKE_KEY, PRIVATE_BODY, PRIVATE_HEADER, PRIVATE_METADATA,
        PRIVATE_NETWORK, PRIVATE_CONTENT, PRIVATE_QUESTION, "upstream_status", "category",
    ):
        assert marker not in public
    assert capsys.readouterr() == ("", "")


def test_caller_cancellation_of_pending_http_propagates_without_timeout_or_retry(context):
    requests, cancellations = [], []

    async def scenario():
        entered = asyncio.Event()

        async def stalled(request):
            requests.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(True)
                raise
            raise AssertionError("A cancelled operation must not finish.")

        task = asyncio.create_task(Client(transport=httpx.MockTransport(stalled)).request(context))
        await entered.wait()
        task.cancel(PRIVATE_NETWORK)
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert type(caught.value) is asyncio.CancelledError
        assert caught.value.args == (PRIVATE_NETWORK,)
        assert task.cancelled()

    asyncio.run(scenario())
    assert len(requests) == 1 and cancellations == [True]


class SyntheticBaseFailure(BaseException):
    pass


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError, SyntheticBaseFailure])
def test_cancellation_and_unclassified_failures_propagate_exactly_once(error_type, context):
    original = error_type(PRIVATE_NETWORK)
    requests = []

    def fail(request):
        requests.append(request)
        raise original

    with pytest.raises(error_type) as caught:
        run_request(httpx.MockTransport(fail), context)
    assert caught.value is original
    assert len(requests) == 1


def test_prompt_builder_failure_propagates_before_http(context, monkeypatch):
    original = RuntimeError("synthetic prompt builder failure")
    calls = []

    def fail(supplied_context):
        assert supplied_context is context
        calls.append("prompt")
        raise original

    def forbidden_http(request):
        raise AssertionError("A failed prompt must not open a provider request.")

    monkeypatch.setattr(provider, "build_semantic_diagnosis_prompt", fail)
    with pytest.raises(RuntimeError) as caught:
        run_request(httpx.MockTransport(forbidden_http), context)
    assert caught.value is original
    assert calls == ["prompt"]


def test_provider_integrates_with_existing_parser_without_owning_semantic_validation(
    context, diagnosis, monkeypatch,
):
    raw = diagnosis.model_dump_json()
    actual_parse = client_boundary.parse_semantic_diagnosis_json
    parsed = []
    requests = []

    def parse(payload):
        assert payload == raw
        result = actual_parse(payload)
        parsed.append(result)
        return result

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(raw))

    monkeypatch.setattr(client_boundary, "parse_semantic_diagnosis_json", parse)
    client = Client(transport=httpx.MockTransport(respond))
    adapter = JSONSemanticDiagnosisAdapter(client)
    result = asyncio.run(adapter.diagnose(context))
    assert len(requests) == len(parsed) == 1
    assert result is parsed[0]
    assert result == diagnosis
    sent = json.loads(requests[0].content)
    user = json.loads(sent["messages"][1]["content"])
    assert set(user) == {"question", "answer", "response_schema"}
    assert user["question"] == context.question
    assert user["answer"] == context.answer
    assert user["response_schema"] == SemanticDiagnosis.model_json_schema()


def test_malformed_semantic_content_is_rejected_only_by_existing_parser(context):
    raw = "```json\n{bad synthetic semantic JSON}\n```"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(raw))

    adapter = JSONSemanticDiagnosisAdapter(Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(SemanticDiagnosisJSONContractError) as caught:
        asyncio.run(adapter.diagnose(context))
    assert type(caught.value) is SemanticDiagnosisJSONContractError
    assert str(caught.value) == "Semantic diagnosis output did not match the required contract."
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(requests) == 1


def test_fake_reader_orchestration_returns_original_context_and_parsed_model_once(
    context, diagnosis, monkeypatch,
):
    original_context = context.model_dump()
    raw = diagnosis.model_dump_json()
    events = []
    reader_calls = []
    requests = []
    parsed = []
    actual_build = provider.build_semantic_diagnosis_prompt
    actual_parse = client_boundary.parse_semantic_diagnosis_json
    session_id = UUID("00000000-0000-4000-8000-000000000017")

    class FakeReader:
        def get_diagnosis_context(
            self, session_id: UUID, question_index: int, attempt_number: int,
        ) -> DiagnosisContext:
            events.append("read")
            reader_calls.append((session_id, question_index, attempt_number))
            return context

    def build(supplied_context):
        assert supplied_context is context
        events.append("prompt")
        return actual_build(supplied_context)

    def respond(request):
        events.append("http")
        requests.append(request)
        return httpx.Response(200, json=envelope(raw))

    def parse(payload):
        assert payload == raw
        events.append("parse")
        result = actual_parse(payload)
        parsed.append(result)
        return result

    monkeypatch.setattr(provider, "build_semantic_diagnosis_prompt", build)
    monkeypatch.setattr(client_boundary, "parse_semantic_diagnosis_json", parse)
    adapter = JSONSemanticDiagnosisAdapter(Client(transport=httpx.MockTransport(respond)))
    result_context, result_diagnosis = asyncio.run(diagnose_persisted_attempt(
        FakeReader(), adapter, session_id=session_id,
        question_index=context.question_index, attempt_number=context.attempt_number,
    ))
    assert events == ["read", "prompt", "http", "parse"]
    assert reader_calls == [(session_id, 4, 2)]
    assert len(requests) == len(parsed) == 1
    assert result_context is context
    assert result_diagnosis is parsed[0]
    assert result_diagnosis == diagnosis
    assert context.model_dump() == original_context


def test_source_stays_transport_only_with_closed_imports_and_no_retry_or_state_policy():
    tree = ast.parse(Path(provider.__file__).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.append(node.module)
    assert set(imports) <= {
        "asyncio", "os", "httpx", "app.diagnosis", "app.semantic_diagnosis_prompt",
        "app.semantic_diagnosis_failure",
    }
    assert {"httpx", "app.semantic_diagnosis_prompt"} <= set(imports)
    assert not any(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(tree))
    forbidden_calls = {
        "print", "open", "eval", "exec", "compile", "sleep", "retry", "backoff",
        "model_validate", "model_validate_json", "model_dump", "model_dump_json",
        "model_copy", "model_construct", "loads", "dumps", "read_text", "write_text",
        "replace", "removeprefix", "removesuffix", "split", "splitlines", "lstrip", "rstrip",
        "commit", "execute", "add", "delete", "flush", "score", "calculate",
        "parse_semantic_diagnosis_json", "evaluate_semantic_diagnosis",
        "info", "debug", "warning", "error", "exception", "critical",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            assert name not in forbidden_calls
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id != "context", "Context field access belongs to the prompt builder."
            if node.value.id == "os":
                assert node.attr == "environ", "Only request-time API-key configuration belongs here."
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            caught_names = {part.id for part in ast.walk(node.type) if isinstance(part, ast.Name)}
            assert not (caught_names & {"Exception", "BaseException"})
    environment_reads = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get" and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os" and node.func.value.attr == "environ"
    ]
    assert len(environment_reads) == 1
    assert isinstance(environment_reads[0].args[0], ast.Constant)
    assert environment_reads[0].args[0].value == "NVIDIA_API_KEY"
    request_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "request"
    )
    assert environment_reads[0] in list(ast.walk(request_node))
    deadlines = [
        node for node in ast.walk(request_node)
        if isinstance(node, ast.AsyncWith)
        and isinstance(node.items[0].context_expr, ast.Call)
        and isinstance(node.items[0].context_expr.func, ast.Attribute)
        and isinstance(node.items[0].context_expr.func.value, ast.Name)
        and node.items[0].context_expr.func.value.id == "asyncio"
    ]
    assert len(deadlines) == 1
    deadline = deadlines[0]
    assert ast.dump(deadline.items[0].context_expr) == ast.dump(ast.Call(
        func=ast.Attribute(value=ast.Name(id="asyncio", ctx=ast.Load()), attr="timeout", ctx=ast.Load()),
        args=[ast.Name(id="NVIDIA_SEMANTIC_DIAGNOSIS_TOTAL_TIMEOUT_SECONDS", ctx=ast.Load())],
        keywords=[],
    ))
    posts = [
        node for node in ast.walk(request_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "post"
    ]
    assert len(posts) == 1 and posts[0] in list(ast.walk(deadline))
    all_strings = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (all_strings & {
        "addressed_question", "strengths", "missing_information", "next_focus",
        "structure_feedback", "retry_instruction", "diagnosis_version",
        "measurement", "previous_attempt", "continue", "move_on", "follow_up",
        "score", "confidence", "confidence_score", "FOLLOW_UP", "CLARIFY", "CHALLENGE", "MOVE_ON",
    })

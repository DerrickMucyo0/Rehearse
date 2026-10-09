"""Synthetic Gemini roleplay transport tests; real network is a tripwire."""

import asyncio
import json
import traceback
from types import SimpleNamespace

import httpx
import pytest

from app import gemini_roleplay as provider
from app.roleplay import RoleplayContext, RoleplayQuestion, RoleplayTurn, RoleplayUnavailable
from app.roleplay_client import JSONRoleplayAdapter

FAKE_KEY = "synthetic-gemini-roleplay-key-not-real"
PRIVATE = "SYNTHETIC_PRIVATE_GEMINI_ROLEPLAY_34812"


@pytest.fixture(autouse=True)
def only_mock_transport_and_in_memory_key(monkeypatch):
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ={"GEMINI_API_KEY": FAKE_KEY}))

    async def forbidden_async(*args, **kwargs):
        raise AssertionError("Real network must never be called.")

    def forbidden_sync(*args, **kwargs):
        raise AssertionError("Real network must never be called.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync)


@pytest.fixture
def context():
    return RoleplayContext(
        scenario_type="job_interview", next_question_number=2,
        turns=(RoleplayTurn(question_number=1, question="Synthetic question?", answer=PRIVATE),),
    )


def valid_json():
    return RoleplayQuestion(
        roleplay_version="live-ai-roleplay-v1", next_question="What happened next?",
    ).model_dump_json()


def assert_failure(error, kind, capsys, caplog, status=None):
    assert type(error) is RoleplayUnavailable
    assert error.failure_kind == kind and error.http_status == status
    assert error.args == ("Unable to generate the next roleplay question.",)
    assert error.__dict__ == {}
    public = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert PRIVATE not in public and FAKE_KEY not in public
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.parametrize("key", [None, "", " \t\n "])
def test_missing_key_stops_before_prompt_and_http(key, context, monkeypatch, capsys, caplog):
    environment = {} if key is None else {"GEMINI_API_KEY": key}
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ=environment))

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing configuration must stop before prompt or HTTP.")

    monkeypatch.setattr(provider, "build_roleplay_prompt", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient().request(context))
    assert_failure(caught.value, "unavailable", capsys, caplog)


def test_stateless_interactions_request_uses_bounded_prompt_and_structured_output(
    context, monkeypatch,
):
    requests, constructions = [], []
    real_http = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"output_text": valid_json(), "id": PRIVATE})

    def construct(**kwargs):
        constructions.append(kwargs)
        return real_http(**kwargs)

    client = provider.GeminiRoleplayClient(httpx.MockTransport(respond))
    monkeypatch.setitem(provider.os.environ, "GEMINI_API_KEY", f" \t{FAKE_KEY}\n ")
    monkeypatch.setitem(provider.os.environ, "GEMINI_MODEL", "synthetic-gemini-model")
    monkeypatch.setattr(httpx, "AsyncClient", construct)
    assert asyncio.run(client.request(context)) == valid_json()

    assert len(requests) == len(constructions) == 1
    request = requests[0]
    assert str(request.url) == provider.GEMINI_INTERACTIONS_ENDPOINT
    assert request.headers["x-goog-api-key"] == FAKE_KEY
    payload = json.loads(request.content)
    assert payload["model"] == "synthetic-gemini-model"
    assert payload["store"] is False
    assert payload["system_instruction"]
    assert json.loads(payload["input"])["context"] == context.model_dump(mode="json")
    assert payload["response_format"]["mime_type"] == "application/json"
    assert payload["response_format"]["schema"]["required"] == [
        "roleplay_version", "next_question",
    ]
    timeout = constructions[0]["timeout"]
    assert timeout.connect == timeout.write == timeout.pool == 15
    assert timeout.read == 60
    assert provider.GEMINI_TOTAL_TIMEOUT_SECONDS == 75


def test_interaction_steps_response_is_supported(context):
    content = [{"text": valid_json()}]
    response = httpx.Response(200, json={"steps": [{"content": content}]})
    assert provider._interaction_text(response) == valid_json()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_errors_are_status_only_and_not_retried(status, context, capsys, caplog):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, content=PRIVATE, headers={"x-request-id": PRIVATE})

    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient(httpx.MockTransport(respond)).request(context))
    assert len(calls) == 1
    assert_failure(caught.value, "http_status", capsys, caplog, status)


@pytest.mark.parametrize("payload", [
    {}, [], {"output_text": ""}, {"steps": []},
])
def test_malformed_interaction_envelopes_are_content_free(payload, context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient(transport).request(context))
    assert_failure(caught.value, "response_contract", capsys, caplog)


def test_invalid_http_json_discards_decode_error(context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=PRIVATE))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient(transport).request(context))
    assert_failure(caught.value, "response_contract", capsys, caplog)


def test_invalid_question_json_is_rejected_by_existing_strict_adapter(context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"output_text": PRIVATE}))
    adapter = JSONRoleplayAdapter(provider.GeminiRoleplayClient(transport))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(adapter.generate(context))
    assert_failure(caught.value, "json_contract", capsys, caplog)


@pytest.mark.parametrize("error,kind", [
    (httpx.ConnectError(PRIVATE), "transport"), (httpx.ReadError(PRIVATE), "transport"),
    (httpx.ConnectTimeout(PRIVATE), "timeout"), (httpx.ReadTimeout(PRIVATE), "timeout"),
    (httpx.WriteTimeout(PRIVATE), "timeout"), (httpx.PoolTimeout(PRIVATE), "timeout"),
])
def test_transport_failures_are_private_and_never_retried(error, kind, context, capsys, caplog):
    calls = []

    def fail(request):
        calls.append(request)
        raise error

    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient(httpx.MockTransport(fail)).request(context))
    assert len(calls) == 1
    assert_failure(caught.value, kind, capsys, caplog)


def test_total_deadline_cancels_pending_request_without_retry(context, monkeypatch, capsys, caplog):
    actual_timeout = asyncio.timeout
    calls, deadlines = [], []

    def immediate_timeout(seconds):
        deadlines.append(seconds)
        return actual_timeout(0)

    async def pending(request):
        calls.append(request)
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "asyncio", SimpleNamespace(timeout=immediate_timeout))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.GeminiRoleplayClient(httpx.MockTransport(pending)).request(context))
    assert deadlines == [75] and len(calls) == 1
    assert_failure(caught.value, "timeout", capsys, caplog)


def test_caller_cancellation_propagates_without_retry(context):
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def pending(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            provider.GeminiRoleplayClient(httpx.MockTransport(pending)).request(context),
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) == 1

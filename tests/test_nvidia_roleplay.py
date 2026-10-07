"""Synthetic NVIDIA roleplay transport tests; real network is a tripwire."""

import asyncio
import json
import traceback
from types import SimpleNamespace

import httpx
import pytest

from app import nvidia_roleplay as provider
from app.roleplay import RoleplayContext, RoleplayQuestion, RoleplayTurn, RoleplayUnavailable
from app.roleplay_client import JSONRoleplayAdapter
from app.roleplay_composition import get_roleplay_adapter

FAKE_KEY = "synthetic-roleplay-key-not-real"
PRIVATE = "SYNTHETIC_PRIVATE_PROVIDER_ROLEPLAY_72194"


@pytest.fixture(autouse=True)
def only_mock_transport_and_in_memory_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ={"NVIDIA_API_KEY": FAKE_KEY}))

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


def envelope(content=None):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content or valid_json()}}]}


def valid_json():
    return RoleplayQuestion(roleplay_version="live-ai-roleplay-v1", next_question="What happened next?").model_dump_json()


def assert_failure(error, kind, capsys, caplog, status=None):
    assert type(error) is RoleplayUnavailable
    assert error.failure_kind == kind and error.http_status == status
    assert error.args == ("Unable to generate the next roleplay question.",)
    assert error.__dict__ == {}
    assert error.__cause__ is None and error.__context__ is None
    public = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert PRIVATE not in public and FAKE_KEY not in public
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_factory_does_not_read_key_or_start_http(monkeypatch):
    class ForbiddenEnvironment:
        def get(self, *args):
            raise AssertionError("Composition cannot access credentials.")

    monkeypatch.setattr(provider, "os", SimpleNamespace(environ=ForbiddenEnvironment()))
    adapter = get_roleplay_adapter()
    assert type(adapter) is JSONRoleplayAdapter
    assert type(adapter._client) is provider.NVIDIANemotronRoleplayClient


@pytest.mark.parametrize("key", [None, "", " \t\n "])
def test_missing_key_stops_before_prompt_and_http(key, context, monkeypatch, capsys, caplog):
    environment = {} if key is None else {"NVIDIA_API_KEY": key}
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ=environment))

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing configuration must stop before prompt or HTTP.")

    monkeypatch.setattr(provider, "build_roleplay_prompt", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.NVIDIANemotronRoleplayClient().request(context))
    assert_failure(caught.value, "unavailable", capsys, caplog)


def test_request_settings_deadlines_final_content_and_request_time_key(context, monkeypatch):
    requests, constructions = [], []
    real_http = httpx.AsyncClient
    monkeypatch.delitem(provider.os.environ, "NVIDIA_API_KEY")

    def respond(request):
        requests.append(request)
        result = envelope()
        result["choices"][0]["message"]["reasoning_content"] = PRIVATE
        return httpx.Response(200, json=result)

    def construct(**kwargs):
        constructions.append(kwargs)
        return real_http(**kwargs)

    client = provider.NVIDIANemotronRoleplayClient(httpx.MockTransport(respond))
    monkeypatch.setitem(provider.os.environ, "NVIDIA_API_KEY", f" \t{FAKE_KEY}\n ")
    monkeypatch.setattr(httpx, "AsyncClient", construct)
    assert asyncio.run(client.request(context)) == valid_json()
    assert len(requests) == len(constructions) == 1
    request = requests[0]
    assert str(request.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    payload = json.loads(request.content)
    assert set(payload) == {
        "model", "messages", "temperature", "max_tokens", "stream", "response_format", "chat_template_kwargs",
    }
    assert payload["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert payload["temperature"] == 0.0 and payload["max_tokens"] == 8192
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert json.loads(payload["messages"][1]["content"])["context"] == context.model_dump(mode="json")
    timeout = constructions[0]["timeout"]
    assert timeout.connect == timeout.write == timeout.pool == 60
    assert timeout.read is None
    assert provider.NVIDIA_ROLEPLAY_TOTAL_TIMEOUT_SECONDS == 120


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_http_errors_are_status_only_and_not_retried(status, context, capsys, caplog):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, content=PRIVATE, headers={"x-request-id": PRIVATE})

    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.NVIDIANemotronRoleplayClient(httpx.MockTransport(respond)).request(context))
    assert len(calls) == 1
    assert_failure(caught.value, "http_status", capsys, caplog, status)


@pytest.mark.parametrize("result", [
    {}, [], {"choices": []}, {"choices": [envelope()["choices"][0]] * 2},
    {"choices": [{"finish_reason": "length", "message": {"content": PRIVATE}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {"content": PRIVATE}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": [PRIVATE]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": PRIVATE, "tool_calls": [{"name": PRIVATE}]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": PRIVATE, "function_call": {"name": PRIVATE}}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": PRIVATE, "refusal": PRIVATE}}]},
])
def test_malformed_envelopes_are_content_free(result, context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=result))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.NVIDIANemotronRoleplayClient(transport).request(context))
    assert_failure(caught.value, "response_contract", capsys, caplog)


def test_invalid_http_json_discards_decode_error(context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=PRIVATE))
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(provider.NVIDIANemotronRoleplayClient(transport).request(context))
    assert_failure(caught.value, "response_contract", capsys, caplog)


def test_invalid_200_question_is_rejected_without_publishing_content(context, capsys, caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=envelope(PRIVATE)))
    adapter = JSONRoleplayAdapter(provider.NVIDIANemotronRoleplayClient(transport))
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
        asyncio.run(provider.NVIDIANemotronRoleplayClient(httpx.MockTransport(fail)).request(context))
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
        asyncio.run(provider.NVIDIANemotronRoleplayClient(httpx.MockTransport(pending)).request(context))
    assert deadlines == [120] and len(calls) == 1
    assert_failure(caught.value, "timeout", capsys, caplog)


def test_caller_cancellation_propagates_and_does_not_retry(context):
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def pending(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(provider.NVIDIANemotronRoleplayClient(httpx.MockTransport(pending)).request(context))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) == 1

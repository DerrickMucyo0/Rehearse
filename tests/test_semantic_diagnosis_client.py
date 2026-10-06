"""Offline contracts for one injected raw client call and existing JSON parsing.

Synthetic clients exercise forwarding and error propagation, without providers,
network, persistence, prompts, or a duplicate parser validation matrix.
"""

import ast
import asyncio
import inspect
import json
from pathlib import Path
from typing import Protocol, get_type_hints
from uuid import UUID

import pytest

from app import semantic_diagnosis_adapter, semantic_diagnosis_eval
from app import semantic_diagnosis_client as boundary
from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import DiagnosisContextReader, diagnose_persisted_attempt
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapter, request_semantic_diagnosis
from app.semantic_diagnosis_client import JSONSemanticDiagnosisAdapter, SemanticDiagnosisJSONClient
from app.semantic_diagnosis_json import SemanticDiagnosisJSONContractError, parse_semantic_diagnosis_json


TYPE_ERROR = "Semantic diagnosis JSON payload must be a string."
CONTRACT_ERROR = "Semantic diagnosis output did not match the required contract."
SECRET = "SYNTHETIC_RAW_CLIENT_OUTPUT_8B7C"
TEXT = f"{SECRET}: café / e\u0301 / ﬁ / 中文 / 😀 first\tsecond\nthird"


@pytest.fixture(autouse=True)
def forbid_offline_evaluation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The client adapter must not invoke offline semantic evaluation.")

    for module, name in (
        (semantic_diagnosis_eval, "evaluate_semantic_diagnosis"),
        (semantic_diagnosis_adapter, "run_semantic_diagnosis_eval"),
    ):
        monkeypatch.setattr(module, name, forbidden)
        monkeypatch.setattr(boundary, name, forbidden, raising=False)


def context():
    return DiagnosisContext(
        question=" \tSynthetic question? 中文\n ",
        answer=" \nSynthetic authoritative answer.\t e\u0301 ",
        question_index=0, attempt_number=1, measurement=None, previous_attempt=None,
    )


def diagnosis():
    return SemanticDiagnosis(
        addressed_question="partially", addressed_question_reason=TEXT,
        strengths=(TEXT,), missing_information=(), structure="mixed",
        structure_feedback=TEXT, next_focus="specificity", next_focus_reason=TEXT,
        retry_instruction=TEXT,
    )


class RecordingClient:
    """A structural fake, with no Protocol inheritance or transport machinery."""

    def __init__(self, result, events=None):
        self.result = result
        self.calls = []
        self.events = events if events is not None else []
        self.active = False

    async def request(self, context: DiagnosisContext) -> str:
        self.calls.append(context)
        self.events.append("client_begin")
        self.active = True
        try:
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result
        finally:
            self.active = False
            self.events.append("client_end")


class SentinelFailure(Exception):
    pass


class SentinelBaseFailure(BaseException):
    pass


def assert_one_client_call(client, supplied):
    assert len(client.calls) == 1
    assert client.calls[0] is supplied


def test_client_protocol_and_adapter_signatures_are_exact_structural_contracts():
    assert Protocol in SemanticDiagnosisJSONClient.__bases__
    assert SemanticDiagnosisJSONClient._is_protocol is True
    assert SemanticDiagnosisJSONClient._is_runtime_protocol is False
    for method in (SemanticDiagnosisJSONClient.request, RecordingClient.request):
        assert inspect.iscoroutinefunction(method)
        signature = inspect.signature(method)
        assert tuple(signature.parameters) == ("self", "context")
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
        assert get_type_hints(method) == {"context": DiagnosisContext, "return": str}
    assert inspect.iscoroutinefunction(JSONSemanticDiagnosisAdapter.diagnose)
    assert inspect.signature(JSONSemanticDiagnosisAdapter.diagnose) == inspect.signature(SemanticDiagnosisAdapter.diagnose)
    assert get_type_hints(JSONSemanticDiagnosisAdapter.diagnose) == {
        "context": DiagnosisContext, "return": SemanticDiagnosis,
    }
    constructor = inspect.signature(JSONSemanticDiagnosisAdapter.__init__)
    assert tuple(constructor.parameters) == ("self", "client")
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter in constructor.parameters.values()
    )
    assert get_type_hints(JSONSemanticDiagnosisAdapter.__init__) == {
        "client": SemanticDiagnosisJSONClient, "return": type(None),
    }
    supplied, expected = context(), diagnosis()
    client: SemanticDiagnosisJSONClient = RecordingClient(expected.model_dump_json())
    adapter: SemanticDiagnosisAdapter = JSONSemanticDiagnosisAdapter(client)
    result = asyncio.run(request_semantic_diagnosis(adapter, supplied))
    assert result == expected
    assert_one_client_call(client, supplied)
    assert SemanticDiagnosisJSONClient not in type(client).__mro__
    assert SemanticDiagnosisAdapter not in JSONSemanticDiagnosisAdapter.__mro__


def test_constructor_stores_exact_client_without_accessing_request():
    class UntouchedClient:
        accesses = 0

        @property
        def request(self):
            self.accesses += 1
            raise AssertionError("Construction must not access the raw client's request boundary.")

    client = UntouchedClient()
    adapter = JSONSemanticDiagnosisAdapter(client)
    assert adapter._client is client
    assert client.accesses == 0


def test_client_completes_before_parser_and_adapter_returns_exact_parser_result(monkeypatch):
    supplied, expected, raw = context(), diagnosis(), object()
    events, parser_calls = [], []

    class CheckpointClient(RecordingClient):
        async def request(self, context):
            self.calls.append(context)
            self.active = True
            events.append("client_begin")
            await asyncio.sleep(0)
            self.active = False
            events.append("client_end")
            return self.result

    client = CheckpointClient(raw, events)

    def parser(payload):
        assert client.active is False
        assert events == ["client_begin", "client_end"]
        parser_calls.append(payload)
        events.append("parser")
        return expected

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    result = asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert result is expected
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw
    assert events == ["client_begin", "client_end", "parser"]


def test_valid_raw_json_is_forwarded_exactly_and_preserves_existing_text_contract(monkeypatch):
    supplied, expected = context(), diagnosis()
    payload = " \t\n" + expected.model_dump_json() + "\r\n "
    original = payload[:]
    client = RecordingClient(payload)
    parser_calls, parsed_models = [], []

    def parser(raw):
        parser_calls.append(raw)
        result = parse_semantic_diagnosis_json(raw)
        parsed_models.append(result)
        return result

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    result = asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert type(result) is SemanticDiagnosis
    assert result == expected
    assert result is parsed_models[0]
    assert result.model_dump() == expected.model_dump()
    assert result.addressed_question_reason == result.structure_feedback == TEXT
    assert result.next_focus_reason == result.retry_instruction == TEXT
    assert result.strengths == (TEXT,)
    assert payload == original
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is payload


@pytest.mark.parametrize("kind", (
    "none", "dict", "list", "tuple", "bytes", "bytearray", "memoryview", "int", "float",
    "bool", "object", "diagnosis",
))
def test_invalid_raw_types_reach_existing_parser_unchanged_once(monkeypatch, kind):
    supplied, expected = context(), diagnosis()
    valid = expected.model_dump_json()
    raw = {
        "none": None, "dict": expected.model_dump(), "list": [valid], "tuple": (valid,),
        "bytes": valid.encode(), "bytearray": bytearray(valid.encode()),
        "memoryview": memoryview(valid.encode()), "int": 17, "float": 1.25,
        "bool": True, "object": object(), "diagnosis": expected,
    }[kind]
    client, parser_calls, parser_errors = RecordingClient(raw), [], []

    def parser(payload):
        parser_calls.append(payload)
        try:
            return parse_semantic_diagnosis_json(payload)
        except TypeError as error:
            parser_errors.append(error)
            raise

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    with pytest.raises(TypeError) as caught:
        asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert type(caught.value) is TypeError
    assert caught.value.args == (TYPE_ERROR,)
    assert str(caught.value) == TYPE_ERROR
    assert len(parser_errors) == 1 and caught.value is parser_errors[0]
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw


def test_raw_object_is_never_stringified_formatted_or_inspected(monkeypatch):
    touches = []

    class OpaqueRawOutput:
        def __str__(self):
            touches.append("str")
            raise AssertionError("Raw output must not be coerced.")

        def __repr__(self):
            touches.append("repr")
            raise AssertionError("Raw output must not be formatted.")

        def __bool__(self):
            touches.append("bool")
            raise AssertionError("Raw output must not be inspected.")

    supplied, expected, raw = context(), diagnosis(), OpaqueRawOutput()
    client, parser_calls = RecordingClient(raw), []

    def parser(payload):
        parser_calls.append(payload)
        return expected

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    result = asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))
    assert result is expected
    assert touches == []
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw


@pytest.mark.parametrize("kind", ("malformed", "code_fence", "prose", "extra_field", "invalid_enum", "wrong_type"))
def test_representative_invalid_json_preserves_existing_parser_contract_failure(monkeypatch, capsys, caplog, kind):
    supplied, expected = context(), diagnosis()
    baseline = expected.model_dump(mode="json")
    valid = expected.model_dump_json()
    props = SemanticDiagnosis.model_json_schema()["properties"]
    enum_field = next(field for field, prop in props.items() if "enum" in prop)
    text_field = next(field for field, prop in props.items() if "maxLength" in prop)
    payload = {
        "malformed": valid[:-1], "code_fence": "```json\n" + valid + "\n```",
        "prose": "Synthetic prose " + valid + " trailing prose.",
        "extra_field": json.dumps({**baseline, "unexpected": SECRET}),
        "invalid_enum": json.dumps({**baseline, enum_field: "unknown-synthetic-enum"}),
        "wrong_type": json.dumps({**baseline, text_field: 17}),
    }[kind]
    client, parser_calls, parser_errors = RecordingClient(payload), [], []

    def parser(raw):
        parser_calls.append(raw)
        try:
            return parse_semantic_diagnosis_json(raw)
        except SemanticDiagnosisJSONContractError as error:
            parser_errors.append(error)
            raise

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    with pytest.raises(SemanticDiagnosisJSONContractError) as caught:
        asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert type(caught.value) is SemanticDiagnosisJSONContractError
    assert caught.value.args == (CONTRACT_ERROR,)
    assert str(caught.value) == CONTRACT_ERROR
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)
    assert len(parser_errors) == 1 and caught.value is parser_errors[0]
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is payload
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []


@pytest.mark.parametrize("error_type", (
    RuntimeError, ValueError, TimeoutError, asyncio.TimeoutError, asyncio.CancelledError,
    SentinelFailure, SentinelBaseFailure,
), ids=("runtime", "value", "builtin-timeout", "asyncio-timeout", "cancelled", "custom-exception", "custom-base"))
def test_client_failures_propagate_exactly_without_parser_retry_fallback_or_timeout_policy(monkeypatch, error_type):
    supplied = context()
    original_error = error_type("Synthetic raw-client failure.")
    client, parser_calls = RecordingClient(original_error), []

    def forbidden_parser(payload):
        parser_calls.append(payload)
        raise AssertionError("Parsing must not be attempted after a client failure.")

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", forbidden_parser)
    with pytest.raises(error_type) as caught:
        asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert caught.value is original_error
    assert_one_client_call(client, supplied)
    assert parser_calls == []


@pytest.mark.parametrize("error_type", (
    SemanticDiagnosisJSONContractError, RuntimeError, asyncio.CancelledError, SentinelBaseFailure,
))
def test_parser_failures_propagate_exactly_without_second_client_call(monkeypatch, error_type):
    supplied, raw = context(), object()
    original_error = error_type("Synthetic parser failure.")
    client, parser_calls = RecordingClient(raw), []

    def parser(payload):
        parser_calls.append(payload)
        raise original_error

    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    with pytest.raises(error_type) as caught:
        asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert caught.value is original_error
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw


def test_context_and_parsed_model_are_never_read_transformed_or_revalidated(monkeypatch):
    supplied, expected, raw = context(), diagnosis(), object()
    before = supplied.model_dump(), expected.model_dump()
    client, parser_calls, forbidden_calls = RecordingClient(raw), [], []

    def parser(payload):
        parser_calls.append(payload)
        return expected

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("The adapter must preserve objects without inspecting or converting models.")

    with monkeypatch.context() as guarded:
        guarded.setattr(boundary, "parse_semantic_diagnosis_json", parser)
        for model in (DiagnosisContext, SemanticDiagnosis):
            original = model.__getattribute__
            fields = frozenset(model.model_fields)

            def block_fields(self, name, original=original, fields=fields):
                if name in fields:
                    forbidden()
                return original(self, name)

            guarded.setattr(model, "__getattribute__", block_fields)
            for name in (
                "__init__", "model_validate", "model_validate_json", "model_validate_strings",
                "model_construct", "model_copy", "model_dump", "model_dump_json", "model_json_schema",
            ):
                guarded.setattr(model, name, forbidden)
        guarded.setattr(json, "loads", forbidden)
        guarded.setattr(json, "dumps", forbidden)
        result = asyncio.run(JSONSemanticDiagnosisAdapter(client).diagnose(supplied))

    assert result is expected
    assert forbidden_calls == []
    assert (supplied.model_dump(), expected.model_dump()) == before
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw


def test_fake_orchestration_preserves_context_and_parser_result_without_extra_reads(monkeypatch):
    supplied, expected = context(), diagnosis()
    payload = expected.model_dump_json()
    identifier = UUID("00000000-0000-4000-8000-000000000081")
    read_calls, parser_calls, parsed_models, events = [], [], [], []

    class Reader:
        def get_diagnosis_context(
            self, session_id: UUID, question_index: int, attempt_number: int,
        ) -> DiagnosisContext:
            read_calls.append((session_id, question_index, attempt_number))
            events.append("reader_return")
            return supplied

    class OrderedClient(RecordingClient):
        async def request(self, context):
            assert events == ["reader_return"]
            return await super().request(context)

    def parser(raw):
        parser_calls.append(raw)
        assert events == ["reader_return", "client_begin", "client_end"]
        events.append("parser")
        result = parse_semantic_diagnosis_json(raw)
        parsed_models.append(result)
        return result

    reader: DiagnosisContextReader = Reader()
    client = OrderedClient(payload, events)
    monkeypatch.setattr(boundary, "parse_semantic_diagnosis_json", parser)
    before = supplied.model_dump()
    result = asyncio.run(diagnose_persisted_attempt(
        reader, JSONSemanticDiagnosisAdapter(client), session_id=identifier,
        question_index=0, attempt_number=1,
    ))

    assert type(result) is tuple and len(result) == 2
    assert result[0] is supplied
    assert type(result[1]) is SemanticDiagnosis
    assert result[1] == expected and result[1] is parsed_models[0]
    assert read_calls == [(identifier, 0, 1)] and read_calls[0][0] is identifier
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is payload
    assert events == ["reader_return", "client_begin", "client_end", "parser"]
    assert supplied.model_dump() == before


def test_runtime_imports_and_source_exclude_provider_policy_transformations_and_context_reads():
    tree = ast.parse(Path(boundary.__file__).read_text())
    allowed = {
        "typing": {"Protocol"}, "app.diagnosis": {"DiagnosisContext"},
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
        "app.semantic_diagnosis_json": {"parse_semantic_diagnosis_json"},
    }
    blocked_attributes = {
        *DiagnosisContext.model_fields, "loads", "dumps", "strip", "lstrip", "rstrip",
        "replace", "split", "lower", "casefold", "decode", "encode", "model_copy",
        "model_dump", "model_dump_json", "model_validate", "model_validate_json",
        "model_validate_strings", "model_construct", "model_json_schema", "getenv", "environ",
        "wait_for", "timeout", "post", "get", "put", "patch", "delete", "send", "log",
    }
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar))
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
            assert {name.name for name in node.names} <= allowed[node.module]
            assert all(name.asname is None for name in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr not in blocked_attributes
        if isinstance(node, ast.Name):
            assert not any(word in node.id.lower() for word in (
                "runtime_checkable", "prompt", "system_message", "user_message", "api_key",
                "retry", "backoff", "timeout", "score", "confidence", "ranking", "repair", "extract",
            ))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"str", "isinstance", "issubclass"}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            assert not any(term in lowered for term in (
                "http://", "https://", "nemotron", "nvidia", "openai", "anthropic", "gemini",
            ))
            assert node.value not in {"GET", "POST", "PUT", "PATCH", "DELETE", "system", "user"}


def complete_without_suspension(coroutine):
    """Drive only immediate fakes so guarded clocks cannot catch an event loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        coroutine.close()
        raise AssertionError("The fake client must complete without suspension.")


def test_adapter_with_fakes_has_no_external_side_effects(monkeypatch, capsys, caplog):
    import builtins
    from collections.abc import Mapping
    import datetime
    import io
    import logging
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    import uuid

    supplied, expected, raw = context(), diagnosis(), object()
    client = RecordingClient(raw)
    parser_calls, forbidden_calls = [], []

    def parser(payload):
        parser_calls.append(payload)
        return expected

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("Only the injected raw client and existing parser are adapter boundaries.")

    class ForbiddenDateTime(datetime.datetime):
        now = classmethod(forbidden)
        utcnow = classmethod(forbidden)
        today = classmethod(forbidden)

    class ForbiddenDate(datetime.date):
        today = classmethod(forbidden)

    class ForbiddenEnvironment(Mapping):
        __getitem__ = forbidden
        __iter__ = forbidden
        __len__ = forbidden
        get = forbidden
        copy = forbidden

    with monkeypatch.context() as guarded:
        guarded.setattr(boundary, "parse_semantic_diagnosis_json", parser)
        for owner, names in (
            (builtins, ("open", "print")), (io, ("open",)),
            (os, ("open", "listdir", "scandir", "stat", "getenv", "putenv", "unsetenv", "urandom")),
            (Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes", "iterdir")),
            (socket, ("socket", "create_connection", "getaddrinfo")),
            (urllib.request, ("urlopen",)),
            (subprocess, ("Popen", "run", "call", "check_call", "check_output")),
            (uuid, ("UUID", "uuid1", "uuid3", "uuid4", "uuid5")),
            (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time")),
            (logging.Logger, ("_log",)),
        ):
            for name in names:
                guarded.setattr(owner, name, forbidden)
        guarded.setattr(os, "environ", ForbiddenEnvironment())
        guarded.setattr(datetime, "datetime", ForbiddenDateTime)
        guarded.setattr(datetime, "date", ForbiddenDate)
        for module_name, names in (
            ("sqlalchemy", ("create_engine",)),
            ("sqlalchemy.engine", ("create_engine",)),
            ("app.database", ("create_database_engine", "create_session_factory")),
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                for name in names:
                    if name in vars(module):
                        guarded.setattr(module, name, forbidden)
        for module_name, class_name, names in (
            ("sqlalchemy.engine", "Engine", ("connect", "begin")),
            ("sqlalchemy.engine", "Connection", ("execute", "begin")),
            ("sqlalchemy.orm", "Session", ("execute", "begin", "add", "flush", "commit")),
            ("sqlalchemy.ext.asyncio", "AsyncSession", ("execute", "begin")),
            ("httpx", "Client", ("request", "send")),
            ("httpx", "AsyncClient", ("request", "send")),
            ("requests", "Session", ("request",)),
            ("openai", "OpenAI", ("__init__",)),
            ("openai", "AsyncOpenAI", ("__init__",)),
            ("openai.resources.chat.completions.completions", "Completions", ("create",)),
            ("openai.resources.chat.completions.completions", "AsyncCompletions", ("create",)),
            ("anthropic", "Anthropic", ("__init__",)),
            ("anthropic", "AsyncAnthropic", ("__init__",)),
            ("anthropic.resources.messages.messages", "Messages", ("create",)),
            ("anthropic.resources.messages.messages", "AsyncMessages", ("create",)),
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                for name in names:
                    guarded.setattr(owner, name, forbidden)
        guarded.setattr(builtins, "__import__", forbidden)
        adapter = JSONSemanticDiagnosisAdapter(client)
        result = complete_without_suspension(adapter.diagnose(supplied))

    # Restore imports, clocks, and environment before pytest assertions.
    assert forbidden_calls == []
    assert result is expected
    assert_one_client_call(client, supplied)
    assert len(parser_calls) == 1 and parser_calls[0] is raw
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []

"""Offline contracts for the small production dependency composition boundary."""

import ast
import asyncio
import builtins
import datetime
import inspect
import logging
import os
from pathlib import Path
import random
import socket
import subprocess
import time
from typing import get_type_hints
import urllib.request
import uuid

import httpx
import pytest
import sqlalchemy
from sqlalchemy import orm

from app import database, semantic_diagnosis_composition as composition, sessions
from app import semantic_diagnosis_client as client_boundary
from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import diagnose_persisted_attempt
from app.nvidia_semantic_diagnosis import NVIDIANemotronSemanticDiagnosisClient
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapter
from app.semantic_diagnosis_client import JSONSemanticDiagnosisAdapter


@pytest.fixture(autouse=True)
def no_key_or_provider_requests(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    async def forbidden_provider(*args, **kwargs):
        raise AssertionError("Composition tests must never invoke the NVIDIA provider.")

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Composition tests must never use real HTTP transport.")

    monkeypatch.setattr(NVIDIANemotronSemanticDiagnosisClient, "request", forbidden_provider)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_provider)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_network)


def test_factory_has_exact_synchronous_zero_argument_contract():
    factory = composition.get_semantic_diagnosis_adapter
    assert inspect.isfunction(factory)
    assert not inspect.iscoroutinefunction(factory)
    assert inspect.signature(factory).parameters == {}
    assert get_type_hints(factory) == {"return": SemanticDiagnosisAdapter}
    assert factory.__defaults__ is None
    assert factory.__kwdefaults__ is None


def test_actual_factory_composes_structural_adapter_and_provider_without_configuration():
    adapter: SemanticDiagnosisAdapter = composition.get_semantic_diagnosis_adapter()
    assert type(adapter) is JSONSemanticDiagnosisAdapter
    assert type(adapter._client) is NVIDIANemotronSemanticDiagnosisClient
    assert JSONSemanticDiagnosisAdapter.__bases__ == (object,)
    assert NVIDIANemotronSemanticDiagnosisClient.__bases__ == (object,)
    assert inspect.iscoroutinefunction(adapter.diagnose)
    assert inspect.signature(JSONSemanticDiagnosisAdapter.diagnose) == inspect.signature(
        SemanticDiagnosisAdapter.diagnose,
    )
    assert get_type_hints(JSONSemanticDiagnosisAdapter.diagnose) == get_type_hints(
        SemanticDiagnosisAdapter.diagnose,
    )


def test_each_call_constructs_fresh_adapter_and_raw_client():
    first = composition.get_semantic_diagnosis_adapter()
    second = composition.get_semantic_diagnosis_adapter()
    assert first is not second
    assert first._client is not second._client
    assert type(first) is type(second) is JSONSemanticDiagnosisAdapter
    assert type(first._client) is type(second._client) is NVIDIANemotronSemanticDiagnosisClient


def test_constructors_run_once_in_order_with_exact_provider_and_return_identity(monkeypatch):
    raw_client = object()
    expected_adapter = object()
    events = []

    def provider_constructor(*args, **kwargs):
        assert args == ()
        assert kwargs == {}
        events.append("provider")
        return raw_client

    def adapter_constructor(*args, **kwargs):
        assert len(args) == 1
        assert args[0] is raw_client
        assert kwargs == {}
        events.append("adapter")
        return expected_adapter

    monkeypatch.setattr(composition, "NVIDIANemotronSemanticDiagnosisClient", provider_constructor)
    monkeypatch.setattr(composition, "JSONSemanticDiagnosisAdapter", adapter_constructor)
    assert composition.get_semantic_diagnosis_adapter() is expected_adapter
    assert events == ["provider", "adapter"]


def test_warmed_factory_has_no_environment_external_state_or_output(
    monkeypatch, capsys, caplog,
):
    # Load dependencies before the guard. Only factory construction runs inside it.
    factory = composition.get_semantic_diagnosis_adapter
    factory()
    violations = []

    def forbidden(*args, **kwargs):
        violations.append("external operation")
        raise AssertionError("Dependency composition must only construct its two objects.")

    class GuardedEnvironment:
        __getitem__ = __iter__ = __len__ = __contains__ = forbidden
        get = keys = items = values = copy = forbidden

    class GuardedDateTime(datetime.datetime):
        now = utcnow = today = fromtimestamp = utcfromtimestamp = classmethod(forbidden)

    class GuardedDate(datetime.date):
        today = fromtimestamp = classmethod(forbidden)

    patches = [
        (os, ("getenv", "putenv", "unsetenv", "urandom")),
        (builtins, ("open", "print")),
        (Path, (
            "open", "read_text", "read_bytes", "write_text", "write_bytes", "touch",
            "mkdir", "unlink", "rename", "replace", "iterdir", "glob", "rglob", "stat",
        )),
        (httpx, ("Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport")),
        (socket, ("socket", "socketpair", "create_connection", "getaddrinfo")),
        (urllib.request, ("urlopen", "urlretrieve", "Request", "build_opener")),
        (urllib.request.OpenerDirector, ("open",)),
        (sqlalchemy, ("create_engine", "create_pool_from_url")),
        (sqlalchemy.engine, ("create_engine",)),
        (orm.Session, ("__init__",)),
        (orm.sessionmaker, ("__init__", "__call__")),
        (database, (
            "create_database_engine", "create_session_factory", "get_database_url",
            "get_test_database_url",
        )),
        (sessions.InterviewSessionService, ("__init__",)),
        (time, (
            "time", "time_ns", "monotonic", "monotonic_ns", "perf_counter",
            "perf_counter_ns", "process_time", "thread_time", "sleep",
        )),
        (uuid, ("uuid1", "uuid3", "uuid4", "uuid5", "uuid6", "uuid7", "uuid8")),
        (random, ("random", "randint", "randrange", "choice", "choices", "getrandbits", "seed")),
        (random.Random, ("random", "getrandbits", "seed")),
        (random.SystemRandom, ("random", "getrandbits")),
        (subprocess, ("Popen", "run", "call", "check_call", "check_output")),
        (logging, ("getLogger", "debug", "info", "warning", "error", "exception", "critical", "log")),
        (logging.Logger, ("_log", "log", "handle")),
        (asyncio, (
            "run", "new_event_loop", "get_event_loop", "get_running_loop",
            "create_task", "ensure_future",
        )),
        (asyncio.BaseEventLoop, ("__init__", "create_task", "run_until_complete")),
    ]
    with monkeypatch.context() as guard:
        for target, names in patches:
            for name in names:
                if hasattr(target, name):
                    guard.setattr(target, name, forbidden)
        guard.setattr(datetime, "datetime", GuardedDateTime)
        guard.setattr(datetime, "date", GuardedDate)
        guard.setattr(os, "environ", GuardedEnvironment())
        adapter = factory()
    # Restore clock, environment, IO, and logging before invoking pytest helpers.
    assert violations == []
    assert type(adapter) is JSONSemanticDiagnosisAdapter
    assert type(adapter._client) is NVIDIANemotronSemanticDiagnosisClient
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_factory_fits_orchestration_with_fake_raw_client_and_existing_parser(monkeypatch):
    context = DiagnosisContext(
        question="Describe a synthetic situation, action, and result.",
        answer="I sorted synthetic cards and found the missing card.",
        question_index=2, attempt_number=1, measurement=None, previous_attempt=None,
    )
    expected = SemanticDiagnosis(
        addressed_question="yes",
        addressed_question_reason="The synthetic example answers the question.",
        strengths=("The synthetic action and result are concrete.",),
        missing_information=(), structure="clear",
        structure_feedback="The synthetic example has a clear sequence.",
        next_focus="maintain_strengths",
        next_focus_reason="The synthetic answer is complete and concise.",
        retry_instruction="Keep the synthetic situation, action, and result concise.",
    )
    raw = expected.model_dump_json()
    session_id = uuid.UUID("00000000-0000-4000-8000-000000000011")
    original_context = context.model_dump()
    actual_parser = client_boundary.parse_semantic_diagnosis_json
    events = []
    reader_calls = []
    client_calls = []
    parsed = []

    class FakeRawClient:
        async def request(self, context: DiagnosisContext) -> str:
            events.append("client")
            client_calls.append(context)
            return raw

    class FakeReader:
        def get_diagnosis_context(
            self, session_id: uuid.UUID, question_index: int, attempt_number: int,
        ) -> DiagnosisContext:
            events.append("read")
            reader_calls.append((session_id, question_index, attempt_number))
            return context

    fake_client = FakeRawClient()

    def construct_provider(*args, **kwargs):
        assert args == ()
        assert kwargs == {}
        events.append("construct")
        return fake_client

    def parse(payload):
        assert payload is raw
        events.append("parse")
        result = actual_parser(payload)
        parsed.append(result)
        return result

    monkeypatch.setattr(composition, "NVIDIANemotronSemanticDiagnosisClient", construct_provider)
    monkeypatch.setattr(client_boundary, "parse_semantic_diagnosis_json", parse)
    adapter = composition.get_semantic_diagnosis_adapter()
    assert type(adapter) is JSONSemanticDiagnosisAdapter
    assert adapter._client is fake_client
    coroutine = diagnose_persisted_attempt(
        FakeReader(), adapter, session_id=session_id, question_index=2, attempt_number=1,
    )
    # These synthetic async operations do not suspend and need no event loop.
    try:
        coroutine.send(None)
    except StopIteration as completed:
        result = completed.value
    else:
        coroutine.close()
        raise AssertionError("The synthetic client must finish without suspension.")
    assert events == ["construct", "read", "client", "parse"]
    assert reader_calls == [(session_id, 2, 1)]
    assert len(client_calls) == len(parsed) == 1
    assert client_calls[0] is context
    assert result[0] is context
    assert result[1] is parsed[0]
    assert type(result[1]) is SemanticDiagnosis
    assert result[1] == expected
    assert context.model_dump() == original_context


def test_runtime_ast_is_only_exact_imports_and_the_two_constructor_factory():
    tree = ast.parse(Path(composition.__file__).read_text())
    statements = list(tree.body)
    if statements and isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant):
        assert isinstance(statements[0].value.value, str)
        statements.pop(0)
    expected_imports = {
        "app.nvidia_semantic_diagnosis": "NVIDIANemotronSemanticDiagnosisClient",
        "app.semantic_diagnosis_adapter": "SemanticDiagnosisAdapter",
        "app.semantic_diagnosis_client": "JSONSemanticDiagnosisAdapter",
    }
    assert len(statements) == 4
    imports = statements[:3]
    assert all(isinstance(node, ast.ImportFrom) for node in imports)
    assert {node.module for node in imports} == set(expected_imports)
    for node in imports:
        assert node.level == 0
        assert len(node.names) == 1
        assert node.names[0].name == expected_imports[node.module]
        assert node.names[0].asname is None
    function = statements[3]
    assert isinstance(function, ast.FunctionDef)
    assert function.name == "get_semantic_diagnosis_adapter"
    assert function.decorator_list == []
    assert ast.dump(function.args) == ast.dump(ast.arguments(
        posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[],
        kwarg=None, defaults=[],
    ))
    assert isinstance(function.returns, ast.Name)
    assert function.returns.id == "SemanticDiagnosisAdapter"
    assert len(function.body) == 1
    returned = function.body[0]
    assert isinstance(returned, ast.Return)
    assert ast.dump(returned.value) == ast.dump(ast.Call(
        func=ast.Name(id="JSONSemanticDiagnosisAdapter", ctx=ast.Load()),
        args=[ast.Call(
            func=ast.Name(id="NVIDIANemotronSemanticDiagnosisClient", ctx=ast.Load()),
            args=[], keywords=[],
        )], keywords=[],
    ))

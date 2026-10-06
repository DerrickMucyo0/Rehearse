"""Offline contracts for the provider-neutral application failure boundary."""

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
import traceback
from typing import get_type_hints
import urllib.request
import uuid

import httpx
import pytest
import sqlalchemy
from sqlalchemy import orm

from app import database, sessions
from app import semantic_diagnosis_application as application
from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import (
    DiagnosisContextReader,
    diagnose_context,
    diagnose_persisted_attempt,
)
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
from app.semantic_diagnosis_json import SemanticDiagnosisJSONContractError
from app.sessions import SessionNotFound


SESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000012")
KNOWN_FAILURES = (
    (
        NVIDIASemanticDiagnosisUnavailable("synthetic private unavailable detail"),
        application.SemanticDiagnosisUnavailable,
        "Semantic diagnosis is not configured.",
    ),
    (
        NVIDIASemanticDiagnosisTimeout("synthetic private timeout detail"),
        application.SemanticDiagnosisTimeout,
        "Semantic diagnosis timed out.",
    ),
    (
        NVIDIASemanticDiagnosisFailed("synthetic private provider detail"),
        application.SemanticDiagnosisFailed,
        "Unable to generate semantic diagnosis.",
    ),
    (
        SemanticDiagnosisJSONContractError("synthetic private raw-output detail"),
        application.SemanticDiagnosisFailed,
        "Unable to generate semantic diagnosis.",
    ),
    (
        SemanticDiagnosisAdapterContractError("synthetic private adapter detail"),
        application.SemanticDiagnosisFailed,
        "Unable to generate semantic diagnosis.",
    ),
)
FAILURE_IDS = ("unavailable", "timeout", "provider", "json-contract", "adapter-contract")


@pytest.fixture(autouse=True)
def no_key_or_real_provider_requests(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    async def forbidden_async(*args, **kwargs):
        raise AssertionError("Application tests must not make provider or network requests.")

    def forbidden_sync(*args, **kwargs):
        raise AssertionError("Application tests must not make network requests.")

    monkeypatch.setattr(NVIDIANemotronSemanticDiagnosisClient, "request", forbidden_async)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync)


def context():
    return DiagnosisContext(
        question=" \tSynthetic question?\n中文 😀 ",
        answer=" \nSynthetic authoritative answer.\t e\u0301 ",
        question_index=2, attempt_number=3, measurement=None, previous_attempt=None,
    )


def diagnosis():
    return SemanticDiagnosis(
        addressed_question="partially", addressed_question_reason="Synthetic reason.",
        strengths=("Synthetic strength.",), missing_information=(), structure="mixed",
        structure_feedback="Synthetic structure feedback.", next_focus="specificity",
        next_focus_reason="Synthetic focus reason.", retry_instruction="Synthetic instruction.",
    )


class RecordingReader:
    def __init__(self, result, events=None):
        self.result = result
        self.calls = []
        self.events = events if events is not None else []
        self.active = False

    def get_diagnosis_context(self, session_id, question_index, attempt_number):
        self.calls.append((session_id, question_index, attempt_number))
        self.events.append("reader_begin")
        self.active = True
        try:
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result
        finally:
            self.active = False
            self.events.append("reader_end")


class RecordingAdapter:
    def __init__(self, result, events=None, reader=None):
        self.result = result
        self.calls = []
        self.events = events if events is not None else []
        self.reader = reader

    async def diagnose(self, supplied):
        if self.reader is not None:
            assert self.reader.active is False
        self.calls.append(supplied)
        self.events.append("adapter_begin")
        if isinstance(self.result, BaseException):
            raise self.result
        self.events.append("adapter_end")
        return self.result


class UntouchedAdapter:
    def __init__(self):
        self.accesses = 0

    @property
    def diagnose(self):
        self.accesses += 1
        raise AssertionError("The adapter must not be accessed after a reader failure.")


class PrivateException(Exception):
    pass


class PrivateBaseException(BaseException):
    pass


def run(reader, adapter):
    return asyncio.run(application.diagnose_application_attempt(
        reader, adapter, session_id=SESSION_ID, question_index=2, attempt_number=3,
    ))


def complete_without_suspension(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        raise AssertionError("The synthetic adapter must finish without an event loop.")
    finally:
        coroutine.close()


def test_public_neutral_classes_and_async_signature_match_existing_orchestration():
    for error_type in (
        application.SemanticDiagnosisUnavailable,
        application.SemanticDiagnosisTimeout,
        application.SemanticDiagnosisFailed,
    ):
        assert error_type.__bases__ == (RuntimeError,)
    function = application.diagnose_application_attempt
    assert inspect.iscoroutinefunction(function)
    signature = inspect.signature(function)
    assert signature == inspect.signature(diagnose_persisted_attempt)
    assert tuple(signature.parameters) == (
        "reader", "adapter", "session_id", "question_index", "attempt_number",
    )
    for index, parameter in enumerate(signature.parameters.values()):
        assert parameter.kind is (
            inspect.Parameter.POSITIONAL_OR_KEYWORD if index < 2 else inspect.Parameter.KEYWORD_ONLY
        )
        assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(function) == {
        "reader": DiagnosisContextReader, "adapter": SemanticDiagnosisAdapter,
        "session_id": uuid.UUID, "question_index": int, "attempt_number": int,
        "return": tuple[DiagnosisContext, SemanticDiagnosis],
    }


def test_success_delegates_once_preserving_arguments_and_exact_tuple_without_model_access(monkeypatch):
    supplied, expected = context(), diagnosis()
    expected_tuple = (supplied, expected)
    reader, adapter = object(), object()
    identifier = uuid.UUID("00000000-0000-4000-8000-000000000013")
    question_index, attempt_number = int("1001"), int("2002")
    calls = []

    async def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return expected_tuple

    def forbidden(*args, **kwargs):
        raise AssertionError("The application must not access, copy, serialize, or validate models.")

    monkeypatch.setattr(application, "diagnose_persisted_attempt", observed)
    with monkeypatch.context() as guard:
        for model in (DiagnosisContext, SemanticDiagnosis):
            for name in (
                "__getattribute__", "__copy__", "__deepcopy__", "model_copy",
                "model_dump", "model_dump_json", "model_validate", "model_validate_json",
            ):
                guard.setattr(model, name, forbidden)
        result = complete_without_suspension(application.diagnose_application_attempt(
            reader, adapter, session_id=identifier,
            question_index=question_index, attempt_number=attempt_number,
        ))
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2 and args[0] is reader and args[1] is adapter
    assert set(kwargs) == {"session_id", "question_index", "attempt_number"}
    assert kwargs["session_id"] is identifier
    assert kwargs["question_index"] is question_index
    assert kwargs["attempt_number"] is attempt_number
    assert result is expected_tuple
    assert result[0] is supplied and result[1] is expected


def test_real_orchestration_reads_once_before_adapter_and_preserves_object_identities():
    supplied, expected = context(), diagnosis()
    events = []
    reader = RecordingReader(supplied, events)
    adapter = RecordingAdapter(expected, events, reader)
    result = run(reader, adapter)
    assert events == ["reader_begin", "reader_end", "adapter_begin", "adapter_end"]
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert reader.calls[0][0] is SESSION_ID
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert result[0] is supplied and result[1] is expected


def test_session_not_found_propagates_exact_object_without_adapter_access():
    original = SessionNotFound("Attempt not found.")
    reader, adapter = RecordingReader(original), UntouchedAdapter()
    with pytest.raises(SessionNotFound) as caught:
        run(reader, adapter)
    assert caught.value is original
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert adapter.accesses == 0


def test_context_only_async_signature_matches_context_orchestration():
    function = application.diagnose_application_context
    assert inspect.iscoroutinefunction(function)
    signature = inspect.signature(function)
    assert signature == inspect.signature(diagnose_context)
    assert tuple(signature.parameters) == ("adapter", "context")
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(function) == {
        "adapter": SemanticDiagnosisAdapter,
        "context": DiagnosisContext,
        "return": tuple[DiagnosisContext, SemanticDiagnosis],
    }


def test_context_only_success_delegates_once_preserving_exact_tuple_without_model_access(monkeypatch):
    supplied, expected = context(), diagnosis()
    expected_tuple = (supplied, expected)
    adapter = object()
    calls = []

    async def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return expected_tuple

    def forbidden(*args, **kwargs):
        raise AssertionError("The application must not access, copy, serialize, or validate models.")

    monkeypatch.setattr(application, "diagnose_context", observed)
    with monkeypatch.context() as guard:
        for model in (DiagnosisContext, SemanticDiagnosis):
            for name in (
                "__getattribute__", "__copy__", "__deepcopy__", "model_copy",
                "model_dump", "model_dump_json", "model_validate", "model_validate_json",
            ):
                guard.setattr(model, name, forbidden)
        result = complete_without_suspension(application.diagnose_application_context(adapter, supplied))
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2 and args[0] is adapter and args[1] is supplied
    assert kwargs == {}
    assert result is expected_tuple
    assert result[0] is supplied and result[1] is expected


def test_context_only_real_orchestration_calls_adapter_once_with_exact_objects():
    supplied, expected = context(), diagnosis()
    adapter = RecordingAdapter(expected)
    result = asyncio.run(application.diagnose_application_context(adapter, supplied))
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert adapter.events == ["adapter_begin", "adapter_end"]
    assert result[0] is supplied and result[1] is expected


@pytest.mark.parametrize("original,neutral,message", KNOWN_FAILURES, ids=FAILURE_IDS)
def test_context_only_known_failures_are_private_and_normalized_once(
    original, neutral, message, monkeypatch, capsys, caplog,
):
    supplied = context()
    adapter = RecordingAdapter(original)
    orchestration_calls = []

    async def observed(*args, **kwargs):
        orchestration_calls.append((args, kwargs))
        return await diagnose_context(*args, **kwargs)

    monkeypatch.setattr(application, "diagnose_context", observed)
    with pytest.raises(neutral) as caught:
        asyncio.run(application.diagnose_application_context(adapter, supplied))
    error = caught.value
    assert type(error) is neutral
    assert error.args == (message,)
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__dict__ == {}
    rendered = "".join(traceback.format_exception(error))
    for public_text in (str(error), repr(error), rendered):
        assert "synthetic private" not in public_text
        assert "NVIDIA" not in public_text
        assert original.args[0] not in public_text
    assert len(orchestration_calls) == 1
    args, kwargs = orchestration_calls[0]
    assert len(args) == 2 and args[0] is adapter and args[1] is supplied
    assert kwargs == {}
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.parametrize("error_type", (
    ValueError, RuntimeError, PrivateException, asyncio.CancelledError, PrivateBaseException,
))
def test_context_only_unknown_failures_and_cancellation_propagate_exactly_without_retry(error_type):
    original = error_type("synthetic private unknown detail")
    supplied = context()
    adapter = RecordingAdapter(original)
    with pytest.raises(error_type) as caught:
        asyncio.run(application.diagnose_application_context(adapter, supplied))
    assert caught.value is original
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


@pytest.mark.parametrize("original,neutral,message", KNOWN_FAILURES, ids=FAILURE_IDS)
def test_known_failures_are_private_and_normalized_once_through_real_orchestration(
    original, neutral, message, monkeypatch, capsys, caplog,
):
    supplied = context()
    reader, adapter = RecordingReader(supplied), RecordingAdapter(original)
    orchestration_calls = []

    async def observed(*args, **kwargs):
        orchestration_calls.append((args, kwargs))
        return await diagnose_persisted_attempt(*args, **kwargs)

    monkeypatch.setattr(application, "diagnose_persisted_attempt", observed)
    with pytest.raises(neutral) as caught:
        run(reader, adapter)
    error = caught.value
    assert type(error) is neutral
    assert error.args == (message,)
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__dict__ == {}
    rendered = "".join(traceback.format_exception(error))
    for public_text in (str(error), repr(error), rendered):
        assert "synthetic private" not in public_text
        assert "NVIDIA" not in public_text
        assert original.args[0] not in public_text
    assert len(orchestration_calls) == 1
    assert reader.calls == [(SESSION_ID, 2, 3)]
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.parametrize("origin", ("reader", "adapter"))
@pytest.mark.parametrize("error_type", (
    ValueError, RuntimeError, PrivateException, asyncio.CancelledError, PrivateBaseException,
))
def test_unknown_failures_and_cancellation_propagate_exactly_without_retry(origin, error_type):
    original = error_type("synthetic private unknown detail")
    supplied = context()
    reader = RecordingReader(original if origin == "reader" else supplied)
    adapter = UntouchedAdapter() if origin == "reader" else RecordingAdapter(original)
    with pytest.raises(error_type) as caught:
        run(reader, adapter)
    assert caught.value is original
    assert reader.calls == [(SESSION_ID, 2, 3)]
    if origin == "reader":
        assert adapter.accesses == 0
    else:
        assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


@pytest.mark.parametrize("failure", (None, *range(len(KNOWN_FAILURES))), ids=("success", *FAILURE_IDS))
@pytest.mark.parametrize("entrypoint", ("attempt", "context"))
def test_warmed_application_has_no_external_side_effects_or_output(
    failure, entrypoint, monkeypatch, capsys, caplog,
):
    supplied, expected = context(), diagnosis()
    adapter_result = expected if failure is None else KNOWN_FAILURES[failure][0]
    reader, adapter = RecordingReader(supplied), RecordingAdapter(adapter_result)
    coroutine = (
        application.diagnose_application_attempt(
            reader, adapter, session_id=SESSION_ID, question_index=2, attempt_number=3,
        ) if entrypoint == "attempt"
        else application.diagnose_application_context(adapter, supplied)
    )
    violations = []

    def forbidden(*args, **kwargs):
        violations.append("external operation")
        raise AssertionError("The application boundary must only delegate and normalize known errors.")

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
        (asyncio, ("sleep", "create_task", "ensure_future")),
    ]
    caught = None
    with monkeypatch.context() as guard:
        for target, names in patches:
            for name in names:
                if hasattr(target, name):
                    guard.setattr(target, name, forbidden)
        guard.setattr(datetime, "datetime", GuardedDateTime)
        guard.setattr(datetime, "date", GuardedDate)
        guard.setattr(os, "environ", GuardedEnvironment())
        try:
            result = complete_without_suspension(coroutine)
        except (
            application.SemanticDiagnosisUnavailable,
            application.SemanticDiagnosisTimeout,
            application.SemanticDiagnosisFailed,
        ) as error:
            caught = error
    # Restore global IO, environment, logging, and clocks before pytest helpers.
    assert violations == []
    if failure is None:
        assert caught is None
        assert result[0] is supplied and result[1] is expected
    else:
        _, neutral, message = KNOWN_FAILURES[failure]
        assert type(caught) is neutral and str(caught) == message
        assert caught.__cause__ is None and caught.__context__ is None
    assert reader.calls == ([(SESSION_ID, 2, 3)] if entrypoint == "attempt" else [])
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_runtime_ast_has_only_neutral_exceptions_single_delegation_and_closed_failure_mapping():
    source = Path(application.__file__).read_text()
    tree = ast.parse(source)
    allowed_imports = {
        "uuid": {"UUID"},
        "app.diagnosis": {"DiagnosisContext"},
        "app.diagnosis_orchestration": {
            "DiagnosisContextReader", "diagnose_context", "diagnose_persisted_attempt",
        },
        "app.nvidia_semantic_diagnosis": {
            "NVIDIASemanticDiagnosisUnavailable", "NVIDIASemanticDiagnosisTimeout",
            "NVIDIASemanticDiagnosisFailed",
        },
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
        "app.semantic_diagnosis_adapter": {
            "SemanticDiagnosisAdapter", "SemanticDiagnosisAdapterContractError",
        },
        "app.semantic_diagnosis_json": {"SemanticDiagnosisJSONContractError"},
    }
    statements = list(tree.body)
    assert isinstance(statements.pop(0), ast.Expr)
    imports = statements[:len(allowed_imports)]
    assert all(isinstance(node, ast.ImportFrom) for node in imports)
    assert {node.module for node in imports} == set(allowed_imports)
    for node in imports:
        assert node.level == 0
        assert {name.name for name in node.names} == allowed_imports[node.module]
        assert all(name.asname is None for name in node.names)
    declarations = statements[len(allowed_imports):]
    assert len(declarations) == 5
    neutral_names = {
        "SemanticDiagnosisUnavailable", "SemanticDiagnosisTimeout", "SemanticDiagnosisFailed",
    }
    classes = declarations[:3]
    assert all(isinstance(node, ast.ClassDef) for node in classes)
    assert {node.name for node in classes} == neutral_names
    for node in classes:
        assert ast.dump(node.bases[0]) == ast.dump(ast.Name(id="RuntimeError", ctx=ast.Load()))
        assert len(node.bases) == 1 and node.keywords == [] and node.decorator_list == []
        assert len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
    functions = declarations[3:]
    assert all(isinstance(function, ast.AsyncFunctionDef) for function in functions)
    assert [function.name for function in functions] == [
        "diagnose_application_attempt", "diagnose_application_context",
    ]
    assert all(function.decorator_list == [] for function in functions)
    assert sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)) == 3
    assert sum(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)) == 2
    assert not any(isinstance(node, ast.FunctionDef) for node in ast.walk(tree))
    forbidden_nodes = (
        ast.Import, ast.Attribute, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
        ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    )
    assert not any(isinstance(node, forbidden_nodes) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Constant) and type(node.value) is int for node in ast.walk(tree))
    for function in functions:
        persisted = function.name == "diagnose_application_attempt"
        delegation = "diagnose_persisted_attempt" if persisted else "diagnose_context"
        arguments = ("reader", "adapter") if persisted else ("adapter", "context")
        keywords = ("session_id", "question_index", "attempt_number") if persisted else ()
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        assert len(calls) == 4
        assert all(isinstance(node.func, ast.Name) for node in calls)
        assert {node.func.id for node in calls} == {delegation, *neutral_names}
        orchestration_call = next(node for node in calls if node.func.id == delegation)
        assert ast.dump(orchestration_call) == ast.dump(ast.Call(
            func=ast.Name(id=delegation, ctx=ast.Load()),
            args=[ast.Name(id=name, ctx=ast.Load()) for name in arguments],
            keywords=[ast.keyword(arg=name, value=ast.Name(id=name, ctx=ast.Load())) for name in keywords],
        ))
        awaits = [node for node in ast.walk(function) if isinstance(node, ast.Await)]
        assert len(awaits) == 1 and awaits[0].value is orchestration_call
        tries = [node for node in ast.walk(function) if isinstance(node, ast.Try)]
        assert len(tries) == 1
        guarded = tries[0]
        assert guarded.orelse == [] and guarded.finalbody == []
        assert len(guarded.body) == 1 and isinstance(guarded.body[0], ast.Return)
        assert guarded.body[0].value is awaits[0]
        expected_handlers = (
            ({"NVIDIASemanticDiagnosisUnavailable"}, "unavailable"),
            ({"NVIDIASemanticDiagnosisTimeout"}, "timeout"),
            ({"NVIDIASemanticDiagnosisFailed", "SemanticDiagnosisJSONContractError",
              "SemanticDiagnosisAdapterContractError"}, "failed"),
        )
        assert len(guarded.handlers) == len(expected_handlers)
        for handler, (names, category) in zip(guarded.handlers, expected_handlers):
            assert handler.name is None
            caught_types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            assert all(isinstance(node, ast.Name) for node in caught_types)
            assert {node.id for node in caught_types} == names
            assert len(handler.body) == 1
            assert ast.dump(handler.body[0]) == ast.dump(ast.Assign(
                targets=[ast.Name(id="failure", ctx=ast.Store())], value=ast.Constant(value=category),
            ))
        # Known types are caught without retaining the original; all public raises
        # occur after the handler and contain only their fixed public messages.
        messages = {
            "SemanticDiagnosisUnavailable": "Semantic diagnosis is not configured.",
            "SemanticDiagnosisTimeout": "Semantic diagnosis timed out.",
            "SemanticDiagnosisFailed": "Unable to generate semantic diagnosis.",
        }
        raises = [node for node in ast.walk(function) if isinstance(node, ast.Raise)]
        assert len(raises) == 3
        for node in raises:
            assert node.exc.func.id in messages
            assert node.exc.keywords == []
            assert len(node.exc.args) == 1 and node.exc.args[0].value == messages[node.exc.func.id]
            assert isinstance(node.cause, ast.Constant) and node.cause.value is None
    for forbidden in (
        "FastAPI", "APIRouter", "Depends", "HTTPException", "starlette", "SQLAlchemy",
        "database", "sessions", "os.environ", "httpx", "logging", "retry", "backoff", "sleep",
        "NVIDIANemotronSemanticDiagnosisClient", "get_semantic_diagnosis_adapter",
        "NVIDIA_API_KEY", "integrate.api.nvidia.com", "nemotron-", "prompt", "response body",
        "finish_reason", "tools", "reasoning", "scores", "FOLLOW_UP", "CLARIFY", "CHALLENGE", "MOVE_ON",
        "model_dump", "model_validate", "get_diagnosis_context", "adapter.diagnose",
    ):
        assert forbidden not in source

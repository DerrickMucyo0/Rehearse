"""Offline contracts for an injected semantic adapter and sequential runner."""

import ast
import asyncio
from collections.abc import Sequence
import inspect
from pathlib import Path
from typing import Protocol, get_type_hints

import pytest

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis import SemanticDiagnosis
from app import semantic_diagnosis_adapter as boundary
from app.semantic_diagnosis_adapter import (
    SemanticDiagnosisAdapter,
    SemanticDiagnosisAdapterContractError,
    request_semantic_diagnosis,
    run_semantic_diagnosis_eval,
)


CONTEXT_ERROR = "Semantic diagnosis context must be a DiagnosisContext instance."
OUTPUT_ERROR = "Adapter must return a SemanticDiagnosis instance."


def context(index=0):
    return DiagnosisContext(
        question=f"Synthetic question {index}.",
        answer=f"Synthetic answer {index}.",
        question_index=index,
        attempt_number=1,
        measurement=None,
        previous_attempt=None,
    )


def diagnosis(index=0):
    """Use valid arbitrary output, without evaluating the synthetic answer."""
    return SemanticDiagnosis(
        addressed_question="partially",
        addressed_question_reason=f"Synthetic reason {index}.",
        strengths=(f"Synthetic strength {index}.",),
        missing_information=(),
        structure="mixed",
        structure_feedback=f"Synthetic structure feedback {index}.",
        next_focus="specificity",
        next_focus_reason=f"Synthetic focus reason {index}.",
        retry_instruction=f"Synthetic retry instruction {index}.",
    )


class RecordingAdapter:
    """A structural fake: no provider, client, or Protocol inheritance."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def diagnose(self, supplied_context: DiagnosisContext) -> SemanticDiagnosis:
        index = len(self.calls)
        self.calls.append(supplied_context)
        result = self.results[index]
        if isinstance(result, BaseException):
            raise result
        return result


class UntouchedAdapter:
    def __init__(self):
        self.accesses = 0

    @property
    def diagnose(self):
        self.accesses += 1
        raise AssertionError("The adapter must not be accessed.")


class ObservedSequence(Sequence):
    """Record when each input is reached, including invalid later inputs."""

    def __init__(self, items, events):
        self.items = items
        self.events = events

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __iter__(self):
        for index, item in enumerate(self.items):
            self.events.append(("yield", index))
            yield item


class AdapterFailure(BaseException):
    """Also cover exceptions outside Exception, alongside cancellation."""


def invalid_context(kind, valid_context):
    return {
        "none": None,
        "dict": valid_context.model_dump(),
        "bool": True,
        "int": 1,
        "str": "context",
        "list": [valid_context],
    }[kind]


def invalid_output(kind, valid_context, valid_output):
    return {
        "none": None,
        "dict": valid_output.model_dump(),
        "str": "diagnosis",
        "context": valid_context,
    }[kind]


def assert_same_objects(actual, expected):
    assert len(actual) == len(expected)
    assert all(left is right for left, right in zip(actual, expected))


def test_public_boundary_has_only_provider_neutral_async_signatures():
    assert Protocol in SemanticDiagnosisAdapter.__bases__
    assert SemanticDiagnosisAdapter._is_protocol is True
    assert issubclass(SemanticDiagnosisAdapterContractError, RuntimeError)
    for function, names, annotations in (
        (
            SemanticDiagnosisAdapter.diagnose,
            ("self", "context"),
            {"context": DiagnosisContext, "return": SemanticDiagnosis},
        ),
        (
            request_semantic_diagnosis,
            ("adapter", "context"),
            {
                "adapter": SemanticDiagnosisAdapter,
                "context": DiagnosisContext,
                "return": SemanticDiagnosis,
            },
        ),
        (
            run_semantic_diagnosis_eval,
            ("adapter", "contexts"),
            {
                "adapter": SemanticDiagnosisAdapter,
                "contexts": Sequence[DiagnosisContext],
                "return": tuple[SemanticDiagnosis, ...],
            },
        ),
    ):
        assert inspect.iscoroutinefunction(function)
        signature = inspect.signature(function)
        assert tuple(signature.parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
        assert get_type_hints(function) == annotations


def test_boundary_imports_only_the_protocol_sequence_and_existing_contracts():
    tree = ast.parse(Path(boundary.__file__).read_text())
    allowed = {
        "collections.abc": {"Sequence"},
        "typing": {"Protocol"},
        "app.diagnosis": {"DiagnosisContext"},
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
    }
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Import)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed
            assert {name.name for name in node.names} <= allowed[node.module]
            assert all(name.asname is None for name in node.names)


def test_request_calls_structural_adapter_once_with_exact_context_and_returns_exact_output():
    supplied = context()
    expected = diagnosis()
    adapter = RecordingAdapter([expected])

    result = asyncio.run(request_semantic_diagnosis(adapter, supplied))

    assert result is expected
    assert_same_objects(adapter.calls, [supplied])


@pytest.mark.parametrize("kind", ("none", "dict", "bool", "int", "str", "list"))
def test_request_rejects_non_context_input_before_even_accessing_adapter(kind):
    bad_context = invalid_context(kind, context())
    adapter = UntouchedAdapter()

    with pytest.raises(TypeError) as error:
        asyncio.run(request_semantic_diagnosis(adapter, bad_context))

    assert str(error.value) == CONTEXT_ERROR
    assert adapter.accesses == 0


@pytest.mark.parametrize("kind", ("none", "dict", "str", "context"))
def test_request_rejects_non_semantic_output_without_coercion_or_retry(kind):
    supplied = context()
    bad_output = invalid_output(kind, supplied, diagnosis())
    adapter = RecordingAdapter([bad_output])

    with pytest.raises(SemanticDiagnosisAdapterContractError) as error:
        asyncio.run(request_semantic_diagnosis(adapter, supplied))

    assert str(error.value) == OUTPUT_ERROR
    assert_same_objects(adapter.calls, [supplied])


@pytest.mark.parametrize("error_type", (RuntimeError, ValueError, TypeError, asyncio.CancelledError, AdapterFailure))
def test_request_propagates_exact_adapter_exception_without_retry(error_type):
    supplied = context()
    original_error = error_type("Synthetic adapter failure.")
    adapter = RecordingAdapter([original_error])

    with pytest.raises(error_type) as error:
        asyncio.run(request_semantic_diagnosis(adapter, supplied))

    assert error.value is original_error
    assert_same_objects(adapter.calls, [supplied])


@pytest.mark.parametrize("empty", ([], ()))
def test_runner_returns_empty_tuple_without_accessing_adapter(empty):
    adapter = UntouchedAdapter()

    result = asyncio.run(run_semantic_diagnosis_eval(adapter, empty))

    assert type(result) is tuple
    assert result == ()
    assert adapter.accesses == 0
    assert len(empty) == 0


@pytest.mark.parametrize("container", (list, tuple))
@pytest.mark.parametrize("count", (1, 4))
def test_runner_preserves_input_order_output_identity_and_duplicates(container, count):
    first, second, third = context(2), context(0), context(1)
    output_a, output_b, output_c = diagnosis(0), diagnosis(1), diagnosis(2)
    supplied = container((first, second, first, third)[:count])
    expected = [output_a, output_b, output_a, output_c][:count]
    inputs_before = tuple(supplied)
    input_values_before = [item.model_dump() for item in supplied]
    output_values_before = [item.model_dump() for item in expected]
    adapter = RecordingAdapter(expected)

    result = asyncio.run(run_semantic_diagnosis_eval(adapter, supplied))

    assert type(result) is tuple
    assert_same_objects(result, expected)
    assert_same_objects(adapter.calls, supplied)
    assert_same_objects(supplied, inputs_before)
    assert [item.model_dump() for item in supplied] == input_values_before
    assert [item.model_dump() for item in expected] == output_values_before
    assert_same_objects(adapter.results, expected)


def test_runner_awaits_each_adapter_call_before_reaching_the_next_context():
    supplied = [context(2), context(0), context(1)]
    expected = [diagnosis(index) for index in range(3)]
    events = []

    class CheckpointAdapter(RecordingAdapter):
        async def diagnose(self, supplied_context):
            index = len(self.calls)
            self.calls.append(supplied_context)
            events.append(("begin", index))
            await asyncio.sleep(0)
            events.append(("end", index))
            return self.results[index]

    adapter = CheckpointAdapter(expected)
    sequence = ObservedSequence(supplied, events)

    result = asyncio.run(run_semantic_diagnosis_eval(adapter, sequence))

    assert events == [
        ("yield", 0), ("begin", 0), ("end", 0),
        ("yield", 1), ("begin", 1), ("end", 1),
        ("yield", 2), ("begin", 2), ("end", 2),
    ]
    assert_same_objects(adapter.calls, supplied)
    assert_same_objects(result, expected)
    assert_same_objects(sequence.items, supplied)


@pytest.mark.parametrize("failure_index", range(3))
@pytest.mark.parametrize("error_type", (RuntimeError, ValueError, asyncio.CancelledError, AdapterFailure))
def test_runner_propagates_exact_nth_adapter_exception_and_never_reaches_later_inputs(
    failure_index, error_type,
):
    supplied = [context(index) for index in range(4)]
    expected = [diagnosis(index) for index in range(4)]
    original_error = error_type("Synthetic adapter failure.")
    expected[failure_index] = original_error
    adapter = RecordingAdapter(expected)
    events = []
    sequence = ObservedSequence(supplied, events)

    with pytest.raises(error_type) as error:
        asyncio.run(run_semantic_diagnosis_eval(adapter, sequence))

    assert error.value is original_error
    assert_same_objects(adapter.calls, supplied[:failure_index + 1])
    assert events == [("yield", index) for index in range(failure_index + 1)]
    assert_same_objects(sequence.items, supplied)
    assert expected[failure_index] is original_error


@pytest.mark.parametrize("failure_index", range(3))
@pytest.mark.parametrize("kind", ("none", "dict", "bool", "int", "str", "list"))
def test_runner_rejects_nth_invalid_context_before_that_adapter_call_and_stops(
    failure_index, kind,
):
    supplied = [context(index) for index in range(4)]
    supplied[failure_index] = invalid_context(kind, supplied[failure_index])
    before = tuple(supplied)
    adapter = RecordingAdapter([diagnosis(index) for index in range(4)])
    events = []

    with pytest.raises(TypeError) as error:
        asyncio.run(run_semantic_diagnosis_eval(adapter, ObservedSequence(supplied, events)))

    assert str(error.value) == CONTEXT_ERROR
    assert_same_objects(adapter.calls, supplied[:failure_index])
    assert events == [("yield", index) for index in range(failure_index + 1)]
    assert_same_objects(supplied, before)


@pytest.mark.parametrize("failure_index", range(3))
@pytest.mark.parametrize("kind", ("none", "dict", "str", "context"))
def test_runner_rejects_nth_invalid_output_without_retry_or_later_calls(failure_index, kind):
    supplied = [context(index) for index in range(4)]
    expected = [diagnosis(index) for index in range(4)]
    expected[failure_index] = invalid_output(kind, supplied[failure_index], expected[failure_index])
    before = tuple(expected)
    adapter = RecordingAdapter(expected)
    events = []

    with pytest.raises(SemanticDiagnosisAdapterContractError) as error:
        asyncio.run(run_semantic_diagnosis_eval(adapter, ObservedSequence(supplied, events)))

    assert str(error.value) == OUTPUT_ERROR
    assert_same_objects(adapter.calls, supplied[:failure_index + 1])
    assert events == [("yield", index) for index in range(failure_index + 1)]
    assert_same_objects(expected, before)


def test_runner_current_adapter_failure_wins_over_later_invalid_context():
    supplied = [context(), None, context(2)]
    original_error = RuntimeError("First adapter call failed.")
    adapter = RecordingAdapter([original_error])
    events = []

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run_semantic_diagnosis_eval(adapter, ObservedSequence(supplied, events)))

    assert error.value is original_error
    assert_same_objects(adapter.calls, supplied[:1])
    assert events == [("yield", 0)]


def test_request_and_runner_do_not_serialize_validate_copy_or_reconstruct_models(monkeypatch):
    supplied = [context(0), context(1)]
    expected = [diagnosis(0), diagnosis(1)]
    input_values_before = [item.model_dump() for item in supplied]
    output_values_before = [item.model_dump() for item in expected]
    request_adapter = RecordingAdapter(expected[:1])
    runner_adapter = RecordingAdapter(expected)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("The boundary must preserve existing model objects.")

    with monkeypatch.context() as guarded:
        for model in (DiagnosisContext, SemanticDiagnosis):
            for name in (
                "__init__", "model_validate", "model_validate_json", "model_validate_strings",
                "model_construct", "model_copy", "model_dump", "model_dump_json",
            ):
                guarded.setattr(model, name, forbidden)
        requested = asyncio.run(request_semantic_diagnosis(request_adapter, supplied[0]))
        evaluated = asyncio.run(run_semantic_diagnosis_eval(runner_adapter, supplied))

    assert calls == []
    assert requested is expected[0]
    assert_same_objects(evaluated, expected)
    assert_same_objects(request_adapter.calls, supplied[:1])
    assert_same_objects(runner_adapter.calls, supplied)
    assert [item.model_dump() for item in supplied] == input_values_before
    assert [item.model_dump() for item in expected] == output_values_before


def complete_without_suspension(coroutine):
    """Drive a pure stub directly so event-loop clock calls are outside the test."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        coroutine.close()
        raise AssertionError("The test stub must complete without suspension.")


def test_request_and_runner_do_not_access_external_boundaries(monkeypatch, capsys, caplog):
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

    supplied = [context(0), context(1)]
    expected = [diagnosis(0), diagnosis(1)]
    request_adapter = RecordingAdapter(expected[:1])
    runner_adapter = RecordingAdapter(expected)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("The semantic adapter boundary must use only supplied objects.")

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
        for owner, names in (
            (builtins, ("open", "print")),
            (io, ("open",)),
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

        # Conftest already loads database modules; patch loaded boundaries only.
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
        for module_name, class_name, method_name in (
            ("sqlalchemy.engine", "Connection", "execute"),
            ("sqlalchemy.orm", "Session", "execute"),
            ("sqlalchemy.ext.asyncio", "AsyncSession", "execute"),
            ("httpx", "Client", "request"),
            ("httpx", "AsyncClient", "request"),
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                guarded.setattr(owner, method_name, forbidden)

        # Monkeypatch imports inspect while installing patches; guard imports
        # only once every other guard has been installed.
        guarded.setattr(builtins, "__import__", forbidden)
        request_result = complete_without_suspension(
            request_semantic_diagnosis(request_adapter, supplied[0]),
        )
        runner_result = complete_without_suspension(
            run_semantic_diagnosis_eval(runner_adapter, supplied),
        )

    assert calls == []
    assert request_result is expected[0]
    assert_same_objects(runner_result, expected)
    assert_same_objects(request_adapter.calls, supplied[:1])
    assert_same_objects(runner_adapter.calls, supplied)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []

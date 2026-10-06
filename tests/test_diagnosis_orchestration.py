"""Provider-neutral orchestration contracts and real PostgreSQL read boundaries.

All semantic outputs and persisted measurement values are synthetic fixtures.
Only the integration cases use the repository's isolated PostgreSQL fixtures.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Protocol, get_type_hints
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app import delivery_metrics, semantic_diagnosis_adapter, semantic_diagnosis_eval
from app import diagnosis_orchestration as orchestration
from app import speaking_metrics
from app.comparisons import DeliverySnapshot, MeasurementSnapshot
from app.database_models import (
    MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement,
)
from app.delivery_metrics import DeliveryMetrics
from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import DiagnosisContextReader, diagnose_persisted_attempt
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import (
    SemanticDiagnosisAdapter, SemanticDiagnosisAdapterContractError, request_semantic_diagnosis,
)
from app.sessions import AttemptRequest, InterviewSessionService
from app.speaking_metrics import SpeakingMetrics


SESSION_ID = UUID("00000000-0000-4000-8000-000000000061")
CONTEXT_ERROR = "Semantic diagnosis context must be a DiagnosisContext instance."
OUTPUT_ERROR = "Adapter must return a SemanticDiagnosis instance."


@pytest.fixture(autouse=True)
def forbid_providers_offline_evaluation_and_objective_recalculation(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Orchestration must not call providers, offline evaluation, or objective calculators.")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_blocked)
    for module, name in (
        (semantic_diagnosis_eval, "evaluate_semantic_diagnosis"),
        (semantic_diagnosis_adapter, "run_semantic_diagnosis_eval"),
        (speaking_metrics, "measure_transcription"),
        (speaking_metrics, "validate_transcription_timings"),
        (delivery_metrics, "measure_delivery"),
    ):
        monkeypatch.setattr(module, name, blocked)
        monkeypatch.setattr(orchestration, name, blocked, raising=False)


def context():
    return DiagnosisContext(
        question=" \tSynthetic question?\n中文 😀 ",
        answer=" \nSynthetic authoritative answer.\t e\u0301 ",
        question_index=0, attempt_number=1, measurement=None, previous_attempt=None,
    )


def diagnosis():
    return SemanticDiagnosis(
        addressed_question="partially", addressed_question_reason="Synthetic reason.",
        strengths=("Synthetic strength.",), missing_information=(), structure="mixed",
        structure_feedback="Synthetic structure feedback.", next_focus="specificity",
        next_focus_reason="Synthetic focus reason.", retry_instruction="Synthetic retry instruction.",
    )


class RecordingReader:
    """A structural fake, without Protocol inheritance or storage methods."""

    def __init__(self, result, events=None):
        self.result = result
        self.calls = []
        self.events = events if events is not None else []
        self.active = False

    def get_diagnosis_context(
        self, session_id: UUID, question_index: int, attempt_number: int,
    ) -> DiagnosisContext:
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

    async def diagnose(self, supplied: DiagnosisContext) -> SemanticDiagnosis:
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
        raise AssertionError("The adapter must not be accessed.")


class SentinelFailure(Exception):
    pass


class SentinelBaseFailure(BaseException):
    pass


def run(reader, adapter, *, session_id=SESSION_ID, question_index=0, attempt_number=1):
    return asyncio.run(diagnose_persisted_attempt(
        reader, adapter, session_id=session_id, question_index=question_index,
        attempt_number=attempt_number,
    ))


def assert_reader_called_once(reader, session_id=SESSION_ID, question_index=0, attempt_number=1):
    assert len(reader.calls) == 1
    actual = reader.calls[0]
    assert len(actual) == 3
    assert all(left is right for left, right in zip(actual, (session_id, question_index, attempt_number)))


def test_reader_protocol_and_service_have_exact_matching_structural_contracts():
    assert Protocol in DiagnosisContextReader.__bases__
    assert DiagnosisContextReader._is_protocol is True
    assert DiagnosisContextReader._is_runtime_protocol is False
    for method in (
        DiagnosisContextReader.get_diagnosis_context,
        InterviewSessionService.get_diagnosis_context,
        RecordingReader.get_diagnosis_context,
    ):
        assert not inspect.iscoroutinefunction(method)
        signature = inspect.signature(method)
        assert tuple(signature.parameters) == ("self", "session_id", "question_index", "attempt_number")
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
        assert get_type_hints(method) == {
            "session_id": UUID, "question_index": int, "attempt_number": int,
            "return": DiagnosisContext,
        }
    # Annotation-based structural use needs neither inheritance nor runtime checks.
    supplied, expected = context(), diagnosis()
    reader: DiagnosisContextReader = RecordingReader(supplied)
    adapter: SemanticDiagnosisAdapter = RecordingAdapter(expected)
    result = run(reader, adapter)
    assert result[0] is supplied and result[1] is expected
    assert DiagnosisContextReader not in type(reader).__mro__
    assert SemanticDiagnosisAdapter not in type(adapter).__mro__


def test_orchestration_public_signature_is_async_and_provider_neutral():
    assert inspect.iscoroutinefunction(diagnose_persisted_attempt)
    signature = inspect.signature(diagnose_persisted_attempt)
    assert tuple(signature.parameters) == (
        "reader", "adapter", "session_id", "question_index", "attempt_number",
    )
    for index, parameter in enumerate(signature.parameters.values()):
        assert parameter.kind is (
            inspect.Parameter.POSITIONAL_OR_KEYWORD if index < 2 else inspect.Parameter.KEYWORD_ONLY
        )
        assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(diagnose_persisted_attempt) == {
        "reader": DiagnosisContextReader, "adapter": SemanticDiagnosisAdapter,
        "session_id": UUID, "question_index": int, "attempt_number": int,
        "return": tuple[DiagnosisContext, SemanticDiagnosis],
    }


def test_reader_arguments_are_forwarded_once_without_normalization_or_replacement():
    class ForwardedInt(int):
        def __int__(self):
            raise AssertionError("Forwarded indices must not be normalized.")

        def __index__(self):
            raise AssertionError("Forwarded indices must not be normalized.")

    identifier = UUID("00000000-0000-4000-8000-000000000062")
    question_index, attempt_number = ForwardedInt(1001), ForwardedInt(2002)
    supplied, expected = context(), diagnosis()
    reader, adapter = RecordingReader(supplied), RecordingAdapter(expected)

    result = run(reader, adapter, session_id=identifier, question_index=question_index, attempt_number=attempt_number)

    assert_reader_called_once(reader, identifier, question_index, attempt_number)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    assert type(result) is tuple and len(result) == 2
    assert result[0] is supplied and result[1] is expected


def test_reader_fully_returns_before_existing_boundary_and_adapter_begin(monkeypatch):
    supplied, expected = context(), diagnosis()
    events, boundary_calls = [], []
    reader = RecordingReader(supplied, events)

    class CheckpointAdapter(RecordingAdapter):
        async def diagnose(self, supplied_context):
            assert reader.active is False
            assert events == ["reader_begin", "reader_end", "boundary_begin"]
            self.calls.append(supplied_context)
            events.append("adapter_begin")
            await asyncio.sleep(0)
            events.append("adapter_end")
            return self.result

    adapter = CheckpointAdapter(expected, events, reader)

    async def observed_boundary(supplied_adapter, supplied_context):
        assert reader.active is False
        assert supplied_context is supplied
        boundary_calls.append((supplied_adapter, supplied_context))
        events.append("boundary_begin")
        result = await request_semantic_diagnosis(supplied_adapter, supplied_context)
        events.append("boundary_end")
        return result

    monkeypatch.setattr(orchestration, "request_semantic_diagnosis", observed_boundary)
    result = run(reader, adapter)

    assert events == [
        "reader_begin", "reader_end", "boundary_begin", "adapter_begin", "adapter_end", "boundary_end",
    ]
    assert_reader_called_once(reader)
    assert len(boundary_calls) == len(adapter.calls) == 1
    assert boundary_calls[0][0] is adapter and boundary_calls[0][1] is supplied
    assert result[0] is supplied and result[1] is expected


@pytest.mark.parametrize("error_type", (
    RuntimeError, ValueError, SentinelFailure, asyncio.CancelledError, SentinelBaseFailure,
))
def test_reader_exception_propagates_unchanged_without_adapter_retry_or_fallback(error_type):
    original_error = error_type("Synthetic reader failure.")
    reader, adapter = RecordingReader(original_error), UntouchedAdapter()

    with pytest.raises(error_type) as error:
        run(reader, adapter)

    assert error.value is original_error
    assert_reader_called_once(reader)
    assert adapter.accesses == 0
    assert reader.events == ["reader_begin", "reader_end"]


@pytest.mark.parametrize("kind", ("none", "dict", "str", "object", "diagnosis"))
def test_invalid_reader_output_is_rejected_by_existing_boundary_without_second_read(monkeypatch, kind):
    invalid = {
        "none": None, "dict": context().model_dump(), "str": "Synthetic context.",
        "object": object(), "diagnosis": diagnosis(),
    }[kind]
    reader, adapter = RecordingReader(invalid), UntouchedAdapter()
    boundary_calls = []

    async def observed_boundary(supplied_adapter, supplied_context):
        boundary_calls.append((supplied_adapter, supplied_context))
        return await request_semantic_diagnosis(supplied_adapter, supplied_context)

    monkeypatch.setattr(orchestration, "request_semantic_diagnosis", observed_boundary)
    with pytest.raises(TypeError) as error:
        run(reader, adapter)

    assert type(error.value) is TypeError
    assert str(error.value) == CONTEXT_ERROR
    assert_reader_called_once(reader)
    assert adapter.accesses == 0
    assert len(boundary_calls) == 1
    assert boundary_calls[0][0] is adapter and boundary_calls[0][1] is invalid


@pytest.mark.parametrize("error_type", (
    RuntimeError, SentinelFailure, asyncio.CancelledError, SentinelBaseFailure,
    SemanticDiagnosisAdapterContractError,
))
def test_adapter_exception_propagates_exact_object_without_reread_retry_or_fallback(error_type):
    supplied = context()
    original_error = error_type("Synthetic adapter failure.")
    reader, adapter = RecordingReader(supplied), RecordingAdapter(original_error)

    with pytest.raises(error_type) as error:
        run(reader, adapter)

    assert error.value is original_error
    assert_reader_called_once(reader)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


@pytest.mark.parametrize("kind", ("none", "dict", "str", "context"))
def test_adapter_contract_failure_preserves_existing_exception_type_and_fixed_message(kind):
    supplied = context()
    invalid = {
        "none": None, "dict": diagnosis().model_dump(), "str": "Synthetic diagnosis.", "context": supplied,
    }[kind]
    reader, adapter = RecordingReader(supplied), RecordingAdapter(invalid)

    with pytest.raises(SemanticDiagnosisAdapterContractError) as error:
        run(reader, adapter)

    assert type(error.value) is SemanticDiagnosisAdapterContractError
    assert str(error.value) == OUTPUT_ERROR
    assert_reader_called_once(reader)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


def test_orchestration_uses_no_offline_eval_or_objective_calculators():
    # The autouse guard patches both source functions and potential local aliases.
    supplied, expected = context(), diagnosis()
    reader, adapter = RecordingReader(supplied), RecordingAdapter(expected)
    result = run(reader, adapter)
    assert result[0] is supplied and result[1] is expected
    assert_reader_called_once(reader)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


def test_context_and_diagnosis_are_never_inspected_serialized_copied_or_reconstructed(monkeypatch):
    supplied, expected = context(), diagnosis()
    before = supplied.model_dump(), expected.model_dump()
    reader, adapter = RecordingReader(supplied), RecordingAdapter(expected)
    accesses = []

    def forbidden(*args, **kwargs):
        accesses.append((args, kwargs))
        raise AssertionError("Orchestration must preserve supplied model objects without conversion.")

    with monkeypatch.context() as guarded:
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
                "model_construct", "model_copy", "model_dump", "model_dump_json",
            ):
                guarded.setattr(model, name, forbidden)
        result = run(reader, adapter)

    assert accesses == []
    assert result[0] is supplied and result[1] is expected
    assert (supplied.model_dump(), expected.model_dump()) == before
    assert_reader_called_once(reader)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied


def test_runtime_imports_only_structural_protocol_and_existing_context_semantic_boundary():
    tree = ast.parse(Path(orchestration.__file__).read_text())
    allowed = {
        "typing": {"Protocol"}, "uuid": {"UUID"},
        "app.diagnosis": {"DiagnosisContext"},
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
        "app.semantic_diagnosis_adapter": {"SemanticDiagnosisAdapter", "request_semantic_diagnosis"},
    }
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Import)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed
            assert {name.name for name in node.names} <= allowed[node.module]
            assert all(name.asname is None for name in node.names)
        if isinstance(node, ast.Name):
            assert node.id != "runtime_checkable"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
            assert not any(isinstance(arg, ast.Name) and arg.id == "DiagnosisContextReader" for arg in node.args[1:])


def complete_without_suspension(coroutine):
    """Avoid event-loop clock access while guarding a nonsuspending fake call."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        coroutine.close()
        raise AssertionError("The fake reader and adapter must complete without suspension.")


def test_fake_orchestration_has_no_external_side_effects(monkeypatch, capsys, caplog):
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

    supplied, expected = context(), diagnosis()
    reader, adapter = RecordingReader(supplied), RecordingAdapter(expected)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("The only orchestration boundaries are the reader and semantic request.")

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
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                for name in names:
                    guarded.setattr(owner, name, forbidden)
        guarded.setattr(builtins, "__import__", forbidden)
        result = complete_without_suspension(diagnose_persisted_attempt(
            reader, adapter, session_id=SESSION_ID, question_index=0, attempt_number=1,
        ))

    # Restore all global clock/import/environment guards before pytest assertions.
    assert calls == []
    assert result[0] is supplied and result[1] is expected
    assert_reader_called_once(reader)
    assert len(adapter.calls) == 1 and adapter.calls[0] is supplied
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []


def metrics(number):
    return SpeakingMetrics(
        recognized_word_count=number * 7, um_count=number - 1, uh_count=0,
        filler_unavailable_reason=None, timed_utterance_span_seconds=number * 6.123456789,
        estimated_words_per_minute=68.591975123, timing_unavailable_reason=None,
    )


def delivery(number):
    return DeliveryMetrics(
        pause_count=number, total_pause_duration_seconds=number * 0.734567891,
        longest_pause_seconds=0.734567891, unavailable_reason=None,
    )


def snapshot(values, pauses):
    return MeasurementSnapshot(
        measurement_version=MEASUREMENT_VERSION, measurement_source=values.source,
        **values.model_dump(exclude={"source"}),
        delivery_metrics=DeliverySnapshot(
            **pauses.model_dump(exclude={"version"}), version=pauses.version, source=values.source,
        ),
    )


def stored_rows(factory):
    """Read all persisted columns before and after the orchestration operation."""
    with factory() as database:
        return {
            model.__tablename__: database.execute(select(model.__table__).order_by(model.id)).all()
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


@pytest.mark.parametrize(("scenario", "selected_number"), (
    ("first", 1), ("historical", 2), ("gap", 3),
))
def test_persisted_context_transaction_ends_before_adapter_and_rows_remain_unchanged(
    postgres_engine, postgres_session_factory, monkeypatch, scenario, selected_number,
):
    setup_service = InterviewSessionService(postgres_session_factory)
    created = setup_service.start()
    expected_measurement = None
    if scenario == "first":
        expected_answer = "First authoritative typed answer: café / 中文."
        setup_service.submit_attempt(created.id, 0, AttemptRequest(
            expected_last_attempt_number=0, answer=expected_answer,
        ))
    elif scenario == "historical":
        for number in (1, 2, 3):
            values, pauses = metrics(number), delivery(number)
            identifier = setup_service.create_measurement(
                created.id, 0, values, expected_last_attempt_number=number - 1,
                delivery_metrics=pauses,
            )
            setup_service.submit_attempt(created.id, 0, AttemptRequest(
                expected_last_attempt_number=number - 1,
                answer=f"Authoritative historical answer {number}.", measurement_id=identifier,
            ))
            if number == selected_number:
                expected_measurement = snapshot(values, pauses)
        # A newer unlinked measurement must never replace the selected linkage.
        setup_service.create_measurement(
            created.id, 0, metrics(9), expected_last_attempt_number=3, delivery_metrics=delivery(9),
        )
        expected_answer = "Authoritative historical answer 2."
    else:
        expected_answer = " \nHistorical answer 3 preserves exact stored spacing.\t "
        with postgres_session_factory.begin() as database:
            for number in (1, 3):
                database.execute(insert(QuestionAttempt).values(
                    session_id=created.id, question_index=0, attempt_number=number,
                    answer_text=expected_answer if number == 3 else "Historical answer 1.",
                ))
    before = stored_rows(postgres_session_factory)
    expected_output = diagnosis()

    class ReadSession(Session):
        pass

    service: DiagnosisContextReader = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=ReadSession))
    read_calls, returned, transactions, ended, statements, timeline = [], [], [], [], [], []
    active = set()
    original_read = service.get_diagnosis_context

    def observed_read(session_id, question_index, attempt_number):
        read_calls.append((session_id, question_index, attempt_number))
        timeline.append("reader_begin")
        result = original_read(session_id, question_index, attempt_number)
        returned.append(result)
        timeline.append("reader_return")
        return result

    def transaction_created(database, transaction):
        transactions.append((database, transaction))
        active.add(transaction)
        timeline.append("transaction_begin")

    def transaction_ended(database, transaction):
        ended.append((database, transaction))
        active.remove(transaction)
        timeline.append("transaction_end")

    def observe_statement(connection, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    def blocked(*args, **kwargs):
        pytest.fail("Orchestration must not perform extra public reads, writes, or workflow transitions.")

    class TransactionBoundaryAdapter(RecordingAdapter):
        async def diagnose(self, supplied_context):
            timeline.append("adapter_begin")
            # These assertions run at adapter entry, not merely after completion.
            assert len(returned) == 1 and supplied_context is returned[0]
            assert len(read_calls) == len(transactions) == len(ended) == len(statements) == 1
            assert transactions[0][0] is ended[0][0]
            assert transactions[0][1] is ended[0][1]
            assert active == set()
            assert transactions[0][1].is_active is False
            assert transactions[0][0].in_transaction() is False
            assert timeline == [
                "reader_begin", "transaction_begin", "transaction_end", "reader_return", "adapter_begin",
            ]
            self.calls.append(supplied_context)
            return self.result

    adapter = TransactionBoundaryAdapter(expected_output)
    monkeypatch.setattr(service, "get_diagnosis_context", observed_read)
    for name in ("get_attempts", "get_comparison", "submit_attempt", "continue_question", "create_measurement"):
        monkeypatch.setattr(service, name, blocked)
    event.listen(ReadSession, "after_transaction_create", transaction_created)
    event.listen(ReadSession, "after_transaction_end", transaction_ended)
    event.listen(ReadSession, "before_flush", blocked)
    event.listen(postgres_engine, "before_cursor_execute", observe_statement)
    try:
        result = run(service, adapter, session_id=created.id, question_index=0, attempt_number=selected_number)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe_statement)
        event.remove(ReadSession, "before_flush", blocked)
        event.remove(ReadSession, "after_transaction_end", transaction_ended)
        event.remove(ReadSession, "after_transaction_create", transaction_created)

    assert type(result) is tuple and len(result) == 2
    supplied = result[0]
    assert type(supplied) is DiagnosisContext
    assert supplied is returned[0] and supplied is adapter.calls[0]
    assert result[1] is expected_output
    assert len(read_calls) == len(adapter.calls) == 1
    assert read_calls[0] == (created.id, 0, selected_number)
    assert read_calls[0][0] is created.id
    assert supplied.question == created.questions[0]
    assert supplied.answer == expected_answer
    assert supplied.question_index == 0
    assert supplied.attempt_number == selected_number
    assert supplied.measurement == expected_measurement
    if scenario == "historical":
        assert supplied.previous_attempt.before_attempt_number == 1
        assert supplied.previous_attempt.after_attempt_number == 2
        assert supplied.previous_attempt.speaking.recognized_word_count.before == 7
        assert supplied.previous_attempt.speaking.recognized_word_count.after == 14
        assert supplied.previous_attempt.speaking.recognized_word_count.delta == 7
        assert supplied.previous_attempt.delivery.pause_count.before == 1
        assert supplied.previous_attempt.delivery.pause_count.after == 2
        assert supplied.previous_attempt.delivery.pause_count.delta == 1
        assert supplied.measurement.recognized_word_count == 14
    else:
        assert supplied.previous_attempt is None
    assert len(transactions) == len(ended) == len(statements) == 1
    assert transactions[0][1].parent is None
    assert active == set()
    statement = " ".join(statements[0].split()).upper()
    assert statement.startswith("SELECT ")
    assert "FOR UPDATE" not in statement
    assert stored_rows(postgres_session_factory) == before

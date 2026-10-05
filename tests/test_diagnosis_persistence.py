"""Authoritative diagnosis contexts from isolated, real PostgreSQL state.

All measurement facts are synthetic persisted scalars. No provider, semantic
adapter, environment configuration, or transcription calculation is used.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app import delivery_metrics, semantic_diagnosis_adapter, semantic_diagnosis_eval
from app import sessions as session_module
from app import speaking_metrics
from app.comparisons import (
    AttemptComparison, ComparisonMetrics, DeliveryComparison, DeliverySnapshot,
    MeasurementSnapshot, compare_delivery_measurements, compare_measurements,
)
from app.database_models import (
    MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement,
)
from app.delivery_metrics import DeliveryMetrics
from app.diagnosis import DiagnosisContext, build_diagnosis_context
from app.sessions import (
    AttemptRequest, ContinueRequest, InterviewSessionService, SessionNotFound,
)
from app.speaking_metrics import SpeakingMetrics


SPEAKING_METRICS = (
    "recognized_word_count", "um_count", "uh_count",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
)
DELIVERY_METRICS = ("pause_count", "total_pause_duration_seconds", "longest_pause_seconds")
TIMING_REASONS = (
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
)


@pytest.fixture(autouse=True)
def forbid_providers_semantics_and_recalculation(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Diagnosis persistence reads must not call providers, semantics, or measurement calculators.")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_blocked)
    for module, name in (
        (speaking_metrics, "measure_transcription"),
        (delivery_metrics, "measure_delivery"),
        (semantic_diagnosis_adapter, "request_semantic_diagnosis"),
        (semantic_diagnosis_adapter, "run_semantic_diagnosis_eval"),
        (semantic_diagnosis_eval, "evaluate_semantic_diagnosis"),
    ):
        monkeypatch.setattr(module, name, blocked)
        monkeypatch.setattr(session_module, name, blocked, raising=False)


@pytest.fixture
def sessions(postgres_session_factory):
    return InterviewSessionService(postgres_session_factory)


def metrics(**changes):
    values = {
        "recognized_word_count": 10, "um_count": 2, "uh_count": 1,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 12.123456789123,
        "estimated_words_per_minute": 49.491231199,
        "timing_unavailable_reason": None,
    }
    values.update(changes)
    return SpeakingMetrics(**values)


def delivery(**changes):
    values = {
        "pause_count": 2, "total_pause_duration_seconds": 1.234567890123,
        "longest_pause_seconds": 0.734567890123, "unavailable_reason": None,
    }
    values.update(changes)
    return DeliveryMetrics(**values)


def expected_snapshot(values, pauses=None, *, speaking_version=MEASUREMENT_VERSION,
                      delivery_version="pause-metrics-v1"):
    return MeasurementSnapshot(
        measurement_version=speaking_version, measurement_source=values.source,
        **values.model_dump(exclude={"source"}),
        delivery_metrics=(DeliverySnapshot(
            **pauses.model_dump(exclude={"version"}), version=delivery_version, source=values.source,
        ) if pauses is not None else None),
    )


def submit(sessions, identifier, *, question=0, revision=0, answer="Answer", measurement=None):
    return sessions.submit_attempt(identifier, question, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer, measurement_id=measurement,
    )).attempt


def measured_attempt(sessions, identifier, values=None, *, question=0, revision=0,
                     answer="Answer", pauses=None):
    measurement = sessions.create_measurement(
        identifier, question, values or metrics(), expected_last_attempt_number=revision,
        delivery_metrics=pauses,
    )
    return submit(sessions, identifier, question=question, revision=revision,
                  answer=answer, measurement=measurement)


def advance(sessions, identifier, *, question=0, revision=1):
    return sessions.continue_question(identifier, question, ContinueRequest(
        expected_last_attempt_number=revision,
    ))


def stored_rows(factory):
    """Capture every persisted column to detect any change caused by a read."""
    with factory() as database:
        return {
            model.__tablename__: database.execute(
                select(model.__table__).order_by(model.id),
            ).all()
            for model in (StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
        }


def test_first_attempt_uses_exact_stored_question_answer_and_indices(
        sessions, postgres_session_factory, monkeypatch):
    question = " \tPourquoi café?\r\n決定 👩🏽‍💻 e\u0301 \u00a0"
    original_questions = session_module.QUESTIONS
    monkeypatch.setattr(session_module, "QUESTIONS", (question, *original_questions[1:]))
    created = sessions.start()
    request_answer = " \n  Café / 咖啡 — e\u0301\tsecond line\r\n "
    first = submit(sessions, created.id, answer=request_answer)
    created.questions[0] = "Changed returned question"
    monkeypatch.setattr(session_module, "QUESTIONS", ("Replacement question",))

    context = sessions.get_diagnosis_context(created.id, 0, 1)
    with postgres_session_factory() as database:
        stored = database.get(StoredInterviewSession, created.id)
        attempt = database.get(QuestionAttempt, first.id)
        assert context.question == stored.questions[0] == question
        assert context.answer == attempt.answer_text == request_answer.strip()
        assert context.question_index == attempt.question_index == 0
        assert context.attempt_number == attempt.attempt_number == 1
    assert isinstance(context, DiagnosisContext)
    assert context.measurement is None
    assert context.previous_attempt is None


def test_historical_answer_is_projected_without_read_time_normalization(
        sessions, postgres_session_factory):
    created = sessions.start()
    answer = "\n  Café / 咖啡 — e\u0301\t\r\n\u2003"
    # Core insertion models historical persisted text without submission trimming.
    with postgres_session_factory.begin() as database:
        database.execute(insert(QuestionAttempt).values(
            session_id=created.id, question_index=0, attempt_number=1, answer_text=answer,
        ))
    assert sessions.get_diagnosis_context(created.id, 0, 1).answer == answer


def test_requested_first_middle_and_latest_contexts_survive_retry_continue_and_completion(
        sessions, monkeypatch):
    created = sessions.start()
    first = measured_attempt(sessions, created.id, metrics(recognized_word_count=7),
                             answer="First authoritative answer", pauses=delivery())
    first_context = sessions.get_diagnosis_context(created.id, 0, 1)
    second = measured_attempt(sessions, created.id, metrics(recognized_word_count=11), revision=1,
                              answer="Second authoritative answer", pauses=delivery())
    second_context = sessions.get_diagnosis_context(created.id, 0, 2)
    assert second_context.previous_attempt.before_attempt_number == 1
    assert second_context.previous_attempt.after_attempt_number == 2
    third = measured_attempt(sessions, created.id, metrics(recognized_word_count=15), revision=2,
                             answer="Third authoritative answer", pauses=delivery())
    contexts = {number: sessions.get_diagnosis_context(created.id, 0, number) for number in (1, 2, 3)}
    assert contexts[1] == first_context
    assert contexts[2] == second_context
    assert [contexts[number].answer for number in (1, 2, 3)] == [first.answer, second.answer, third.answer]
    assert contexts[3].previous_attempt.before_attempt_number == 2
    assert contexts[3].previous_attempt.after_attempt_number == 3
    assert contexts[3].previous_attempt.speaking.recognized_word_count.delta == 4

    default = sessions.get_comparison(created.id, 0)
    explicit = sessions.get_comparison(created.id, 0, before=2, after=3)
    assert (default.before_attempt.attempt_number, default.after_attempt.attempt_number) == (1, 3)
    assert default.comparison.recognized_word_count.delta == 8
    assert (explicit.before_attempt.attempt_number, explicit.after_attempt.attempt_number) == (2, 3)
    assert contexts[3].previous_attempt.speaking == explicit.comparison
    assert contexts[3].previous_attempt.delivery == explicit.delivery_comparison

    monkeypatch.setattr(session_module, "QUESTIONS", ())
    advanced = advance(sessions, created.id, revision=3)
    assert advanced.current_question_index == 1
    assert {number: sessions.get_diagnosis_context(created.id, 0, number) for number in (1, 2, 3)} == contexts
    for question in range(1, len(created.questions)):
        submit(sessions, created.id, question=question, answer=f"Answer {question}")
        completed = advance(sessions, created.id, question=question)
    assert completed.status == "completed"
    assert {number: sessions.get_diagnosis_context(created.id, 0, number) for number in (1, 2, 3)} == contexts


def test_numbering_gap_has_no_predecessor_or_fallback(sessions, postgres_session_factory):
    created = sessions.start()
    with postgres_session_factory.begin() as database:
        for number in (1, 3):
            database.execute(insert(QuestionAttempt).values(
                session_id=created.id, question_index=0, attempt_number=number,
                answer_text=f"Historical attempt {number}",
            ))
    context = sessions.get_diagnosis_context(created.id, 0, 3)
    assert context.answer == "Historical attempt 3"
    assert context.attempt_number == 3
    assert context.previous_attempt is None
    assert context.measurement is None
    assert sessions.get_comparison(created.id, 0).before_attempt.attempt_number == 1


@pytest.mark.parametrize("attempt_number", [0, -1, -99, 2, 99])
def test_missing_attempt_has_exact_existing_error(sessions, attempt_number):
    created = sessions.start()
    submit(sessions, created.id)
    with pytest.raises(SessionNotFound) as error:
        sessions.get_diagnosis_context(created.id, 0, attempt_number)
    assert str(error.value) == "Attempt not found."


def test_unanswered_question_has_missing_attempt_error(sessions):
    created = sessions.start()
    with pytest.raises(SessionNotFound) as error:
        sessions.get_diagnosis_context(created.id, 0, 1)
    assert str(error.value) == "Attempt not found."


def test_unknown_session_has_exact_existing_error(sessions):
    with pytest.raises(SessionNotFound) as error:
        sessions.get_diagnosis_context(uuid4(), 0, 1)
    assert str(error.value) == "Session not found."


@pytest.mark.parametrize("question_index", [-1, 5, 99])
def test_question_outside_stored_snapshot_has_exact_existing_error(sessions, question_index):
    created = sessions.start()
    submit(sessions, created.id)
    with pytest.raises(SessionNotFound) as error:
        sessions.get_diagnosis_context(created.id, question_index, 1)
    assert str(error.value) == "Question not found."


def test_requested_attempt_cannot_be_selected_from_another_question_or_session(sessions):
    owner, other = sessions.start(), sessions.start()
    submit(sessions, owner.id)
    advance(sessions, owner.id)
    for revision in (0, 1):
        submit(sessions, owner.id, question=1, revision=revision, answer=f"Question one attempt {revision + 1}")
        submit(sessions, other.id, revision=revision, answer=f"Other session attempt {revision + 1}")
    selected = sessions.get_diagnosis_context(owner.id, 1, 2)
    assert selected.question == owner.questions[1]
    assert selected.question_index == 1
    assert selected.answer == "Question one attempt 2"
    with pytest.raises(SessionNotFound) as error:
        sessions.get_diagnosis_context(owner.id, 0, 2)
    assert str(error.value) == "Attempt not found."


def test_only_exact_linked_measurement_supplies_facts(sessions):
    owner, other = sessions.start(), sessions.start()
    sessions.create_measurement(owner.id, 0, metrics(recognized_word_count=999),
                                expected_last_attempt_number=0, delivery_metrics=delivery())
    a_values, a_pauses = metrics(recognized_word_count=7, um_count=0), delivery(
        pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0,
    )
    b_values, b_pauses = metrics(recognized_word_count=11), delivery()
    first = measured_attempt(sessions, owner.id, a_values,
                             answer="An edited answer unrelated to the original word count.", pauses=a_pauses)
    submit(sessions, owner.id, revision=1, answer="Typed attempt without any measurement")
    third = measured_attempt(sessions, owner.id, b_values, revision=2, pauses=b_pauses)
    assert first.measurement_id != third.measurement_id
    sessions.create_measurement(owner.id, 0, metrics(recognized_word_count=888),
                                expected_last_attempt_number=3, delivery_metrics=delivery())
    measured_attempt(sessions, other.id, metrics(recognized_word_count=777), pauses=delivery())
    advance(sessions, owner.id, revision=3)
    measured_attempt(sessions, owner.id, metrics(recognized_word_count=666), question=1, pauses=delivery())

    first_context = sessions.get_diagnosis_context(owner.id, 0, 1)
    typed_context = sessions.get_diagnosis_context(owner.id, 0, 2)
    third_context = sessions.get_diagnosis_context(owner.id, 0, 3)
    assert first_context.answer == first.answer
    assert first_context.measurement == expected_snapshot(a_values, a_pauses)
    assert first_context.previous_attempt is None
    assert typed_context.measurement is None
    assert third_context.measurement == expected_snapshot(b_values, b_pauses)
    assert first_context.measurement.measurement_source == "original_transcription"
    assert first_context.measurement.delivery_metrics.source == "original_transcription"
    assert typed_context.previous_attempt.speaking.recognized_word_count.after_unavailable_reason == "no_measurement"
    assert third_context.previous_attempt.speaking.recognized_word_count.before_unavailable_reason == "no_measurement"


@pytest.mark.parametrize("state", ["zero_counts", "zero_delivery"])
def test_recorded_numeric_zero_is_not_unavailable(sessions, state):
    created = sessions.start()
    values = metrics(um_count=0, uh_count=0)
    pauses = delivery(pause_count=0, total_pause_duration_seconds=0.0, longest_pause_seconds=0.0)
    if state == "zero_counts":
        values = metrics(recognized_word_count=0, um_count=0, uh_count=0,
                         timed_utterance_span_seconds=None, estimated_words_per_minute=None,
                         timing_unavailable_reason="missing_timings")
        pauses = None
    measured_attempt(sessions, created.id, values, pauses=pauses)
    context = sessions.get_diagnosis_context(created.id, 0, 1)
    assert context.measurement == expected_snapshot(values, pauses)
    assert context.measurement.um_count == context.measurement.uh_count == 0
    assert context.measurement.filler_unavailable_reason is None
    if pauses is None:
        assert context.measurement.recognized_word_count == 0
        assert context.measurement.delivery_metrics is None
    else:
        assert all(getattr(context.measurement.delivery_metrics, name) == 0 for name in DELIVERY_METRICS)
        assert context.measurement.delivery_metrics.unavailable_reason is None
    assert context.previous_attempt is None


@pytest.mark.parametrize("reason", TIMING_REASONS)
def test_unavailable_measurement_reasons_survive_projection_and_comparison(sessions, reason):
    created = sessions.start()
    before_values, before_pauses = metrics(), delivery()
    after_values = metrics(um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language",
                           timed_utterance_span_seconds=None, estimated_words_per_minute=None,
                           timing_unavailable_reason=reason)
    after_pauses = delivery(pause_count=None, total_pause_duration_seconds=None,
                            longest_pause_seconds=None, unavailable_reason=reason)
    measured_attempt(sessions, created.id, before_values, pauses=before_pauses)
    measured_attempt(sessions, created.id, after_values, revision=1, pauses=after_pauses)
    before, after = expected_snapshot(before_values, before_pauses), expected_snapshot(after_values, after_pauses)
    context = sessions.get_diagnosis_context(created.id, 0, 2)
    assert context.measurement == after
    assert context.previous_attempt.speaking == compare_measurements(before, after)
    assert context.previous_attempt.delivery == compare_delivery_measurements(before, after)
    assert context.previous_attempt.speaking.um_count.after_unavailable_reason == "unsupported_language"
    assert context.previous_attempt.speaking.uh_count.after_unavailable_reason == "unsupported_language"
    for name in SPEAKING_METRICS[-2:]:
        change = getattr(context.previous_attempt.speaking, name)
        assert change.after_unavailable_reason == reason
        assert change.delta is None
        assert change.comparison_unavailable_reason == "after_unavailable"
    for name in DELIVERY_METRICS:
        change = getattr(context.previous_attempt.delivery, name)
        assert change.after_unavailable_reason == reason
        assert change.delta is None
        assert change.comparison_unavailable_reason == "after_unavailable"


@pytest.mark.parametrize(("before_measured", "after_measured", "reason"), [
    (False, True, "before_unavailable"),
    (True, False, "after_unavailable"),
    (False, False, "both_unavailable"),
])
def test_immediate_comparison_exists_when_either_measurement_is_missing(
        sessions, before_measured, after_measured, reason):
    created = sessions.start()
    values, pauses = metrics(), delivery()
    for revision, measured in enumerate((before_measured, after_measured)):
        if measured:
            measured_attempt(sessions, created.id, values, revision=revision, pauses=pauses)
        else:
            submit(sessions, created.id, revision=revision)
    sessions.create_measurement(created.id, 0, metrics(recognized_word_count=999),
                                expected_last_attempt_number=2, delivery_metrics=delivery())
    before = expected_snapshot(values, pauses) if before_measured else None
    after = expected_snapshot(values, pauses) if after_measured else None
    context = sessions.get_diagnosis_context(created.id, 0, 2)
    assert context.measurement == after
    assert context.previous_attempt.before_attempt_number == 1
    assert context.previous_attempt.after_attempt_number == 2
    assert context.previous_attempt.speaking == compare_measurements(before, after)
    assert context.previous_attempt.delivery == compare_delivery_measurements(before, after)
    for family, names in ((context.previous_attempt.speaking, SPEAKING_METRICS),
                          (context.previous_attempt.delivery, DELIVERY_METRICS)):
        for name in names:
            change = getattr(family, name)
            assert change.delta is None
            assert change.comparable is False
            assert change.comparison_unavailable_reason == reason
            assert change.before_unavailable_reason == (None if before_measured else "no_measurement")
            assert change.after_unavailable_reason == (None if after_measured else "no_measurement")


def test_comparison_preserves_signed_and_unrounded_existing_deltas(sessions):
    created = sessions.start()
    before_values, before_pauses = metrics(), delivery()
    after_values = metrics(recognized_word_count=13, um_count=0, uh_count=4,
                           timed_utterance_span_seconds=12.123456789789,
                           estimated_words_per_minute=48.891231199)
    after_pauses = delivery(pause_count=3, total_pause_duration_seconds=1.234567890789,
                            longest_pause_seconds=0.734567889789)
    measured_attempt(sessions, created.id, before_values, pauses=before_pauses)
    measured_attempt(sessions, created.id, after_values, revision=1, pauses=after_pauses)
    context = sessions.get_diagnosis_context(created.id, 0, 2)
    assert isinstance(context.previous_attempt.speaking, ComparisonMetrics)
    assert isinstance(context.previous_attempt.delivery, DeliveryComparison)
    for family, before, after, names in (
        (context.previous_attempt.speaking, before_values, after_values, SPEAKING_METRICS),
        (context.previous_attempt.delivery, before_pauses, after_pauses, DELIVERY_METRICS),
    ):
        for name in names:
            change = getattr(family, name)
            assert change.before == getattr(before, name)
            assert change.after == getattr(after, name)
            assert change.delta == getattr(after, name) - getattr(before, name)
            assert change.comparable is True
    assert context.previous_attempt.speaking.um_count.delta == -2
    assert context.previous_attempt.speaking.recognized_word_count.delta == 3
    assert context.previous_attempt.speaking.estimated_words_per_minute.delta < 0
    assert context.previous_attempt.delivery.longest_pause_seconds.delta < 0
    assert context.previous_attempt.speaking.timed_utterance_span_seconds.delta != round(
        after_values.timed_utterance_span_seconds - before_values.timed_utterance_span_seconds, 3,
    )
    assert context.previous_attempt.delivery.total_pause_duration_seconds.delta != round(
        after_pauses.total_pause_duration_seconds - before_pauses.total_pause_duration_seconds, 3,
    )


@pytest.mark.parametrize(("speaking_version", "delivery_version"), [
    (" speaking-historical-v9 ", "pause-metrics-v1"),
    (MEASUREMENT_VERSION, " pause-historical-v7 "),
])
def test_persisted_provenance_and_independent_metric_family_compatibility(
        sessions, postgres_session_factory, speaking_version, delivery_version):
    created = sessions.start()
    values, pauses = metrics(), delivery()
    measured_attempt(sessions, created.id, values, pauses=pauses)
    # Insert a historical version at creation: measurement rows remain immutable.
    with postgres_session_factory.begin() as database:
        row = TranscriptionMeasurement(
            session_id=created.id, question_index=0, measurement_version=speaking_version,
            measurement_source=values.source, **values.model_dump(exclude={"source"}),
            delivery_measurement_version=delivery_version, pause_count=pauses.pause_count,
            total_pause_duration_seconds=pauses.total_pause_duration_seconds,
            longest_pause_seconds=pauses.longest_pause_seconds, pause_unavailable_reason=None,
        )
        database.add(row)
        database.flush()
        measurement = row.id
    submit(sessions, created.id, revision=1, measurement=measurement)
    context = sessions.get_diagnosis_context(created.id, 0, 2)
    assert context.measurement == expected_snapshot(
        values, pauses, speaking_version=speaking_version, delivery_version=delivery_version,
    )
    assert context.measurement.measurement_version == speaking_version
    assert context.measurement.measurement_source == "original_transcription"
    assert context.measurement.delivery_metrics.version == delivery_version
    assert context.measurement.delivery_metrics.source == "original_transcription"
    assert context.previous_attempt.delivery.before_version == "pause-metrics-v1"
    assert context.previous_attempt.delivery.after_version == delivery_version
    assert context.previous_attempt.delivery.before_source == "original_transcription"
    assert context.previous_attempt.delivery.after_source == "original_transcription"
    for family, names, compatible in (
        (context.previous_attempt.speaking, SPEAKING_METRICS, speaking_version == MEASUREMENT_VERSION),
        (context.previous_attempt.delivery, DELIVERY_METRICS, delivery_version == "pause-metrics-v1"),
    ):
        for name in names:
            change = getattr(family, name)
            assert change.comparable is compatible
            assert change.delta == (0 if compatible else None)
            assert change.comparison_unavailable_reason == (None if compatible else "measurement_version_mismatch")


@pytest.mark.parametrize("attempt_number", [1, 2, 3])
def test_builder_receives_authoritative_projection_and_only_adjacent_comparison(
        sessions, monkeypatch, attempt_number):
    created = sessions.start()
    attempts = [measured_attempt(
        sessions, created.id, metrics(recognized_word_count=number * 10), revision=number - 1,
        answer=f"Authoritative answer {number}", pauses=delivery(),
    ) for number in (1, 2, 3)]
    projected = {}
    builder_calls, speaking_calls, delivery_calls, built = [], [], [], []
    original_project = sessions._measurement_snapshot

    def project(record):
        result = original_project(record)
        projected[record.id] = result
        return result

    def speaking(before, after):
        speaking_calls.append((before, after))
        return compare_measurements(before, after)

    def pauses(before, after):
        delivery_calls.append((before, after))
        return compare_delivery_measurements(before, after)

    def builder(**facts):
        builder_calls.append(facts)
        result = build_diagnosis_context(**facts)
        built.append(result)
        return result

    monkeypatch.setattr(sessions, "_measurement_snapshot", project)
    monkeypatch.setattr(session_module, "compare_measurements", speaking)
    monkeypatch.setattr(session_module, "compare_delivery_measurements", pauses)
    monkeypatch.setattr(session_module, "build_diagnosis_context", builder)
    result = sessions.get_diagnosis_context(created.id, 0, attempt_number)
    assert len(builder_calls) == 1
    assert result is built[0]
    facts = builder_calls[0]
    target = attempts[attempt_number - 1]
    assert facts["question"] == created.questions[0]
    assert facts["answer"] == target.answer
    assert facts["question_index"] == target.question_index
    assert facts["attempt_number"] == target.attempt_number
    assert facts["measurement"] is projected[target.measurement_id]
    assert result.measurement is facts["measurement"]
    if attempt_number == 1:
        assert facts["comparison"] is None
        assert speaking_calls == delivery_calls == []
        assert result.previous_attempt is None
    else:
        predecessor = attempts[attempt_number - 2]
        supplied = facts["comparison"]
        assert isinstance(supplied, AttemptComparison)
        assert supplied.session_id == created.id
        assert supplied.question_index == target.question_index
        assert supplied.before_attempt.id == predecessor.id
        assert supplied.before_attempt.attempt_number == attempt_number - 1
        assert supplied.before_attempt.measurement_id == predecessor.measurement_id
        assert supplied.after_attempt.id == target.id
        assert supplied.after_attempt.attempt_number == attempt_number
        assert supplied.after_attempt.measurement_id == target.measurement_id
        assert len(speaking_calls) == len(delivery_calls) == 1
        for before, after in (speaking_calls[0], delivery_calls[0]):
            assert before is projected[predecessor.measurement_id]
            assert after is facts["measurement"]
        assert result.previous_attempt.speaking is supplied.comparison
        assert result.previous_attempt.delivery is supplied.delivery_comparison


@pytest.mark.parametrize("lifecycle", ["active", "continued", "completed"])
def test_diagnosis_uses_one_unlocked_read_transaction_without_mutation_or_public_reads(
        sessions, postgres_engine, postgres_session_factory, monkeypatch, lifecycle):
    created = sessions.start()
    measured_attempt(sessions, created.id, pauses=delivery())
    measured_attempt(sessions, created.id, metrics(recognized_word_count=13), revision=1, pauses=delivery())
    if lifecycle != "active":
        advance(sessions, created.id, revision=2)
    if lifecycle == "completed":
        for question in range(1, len(created.questions)):
            submit(sessions, created.id, question=question)
            advance(sessions, created.id, question=question)
    before = stored_rows(postgres_session_factory)

    class ReadSession(Session):
        pass

    audited = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=ReadSession))
    transactions, statements = [], []

    def transaction_created(database, transaction):
        transactions.append(transaction)

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def blocked(*args, **kwargs):
        pytest.fail("Diagnosis must use its own read transaction without public service reads or ORM mutations.")

    for name in ("get_attempts", "get_comparison"):
        monkeypatch.setattr(audited, name, blocked)
    event.listen(ReadSession, "after_transaction_create", transaction_created)
    event.listen(ReadSession, "before_flush", blocked)
    event.listen(postgres_engine, "before_cursor_execute", observe)
    try:
        result = audited.get_diagnosis_context(created.id, 0, 2)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe)
        event.remove(ReadSession, "before_flush", blocked)
        event.remove(ReadSession, "after_transaction_create", transaction_created)

    assert result.attempt_number == 2
    assert result.previous_attempt.before_attempt_number == 1
    assert len(transactions) == 1
    assert transactions[0].parent is None
    assert len(statements) == 1
    statement = " ".join(statements[0].split())
    assert statement.upper().startswith("SELECT ")
    assert "FOR UPDATE" not in statement.upper()
    assert "transcription_measurements.id = question_attempts.measurement_id" in statement
    assert "transcription_measurements.session_id = question_attempts.session_id" in statement
    assert "transcription_measurements.question_index = question_attempts.question_index" in statement
    assert stored_rows(postgres_session_factory) == before


@pytest.mark.parametrize("attempt_count", [0, 1])
def test_shared_read_preserves_comparison_defaults_with_fewer_than_two_attempts(sessions, attempt_count):
    created = sessions.start()
    first = measured_attempt(sessions, created.id) if attempt_count else None
    comparison = sessions.get_comparison(created.id, 0)
    if first is None:
        assert comparison.before_attempt is None
    else:
        assert comparison.before_attempt.id == first.id
    assert comparison.after_attempt is None
    assert comparison.comparison is None
    assert comparison.delivery_comparison is None

"""Persisted attempt comparisons against isolated, real PostgreSQL.

Synthetic metrics are inserted through the normal persistence boundary. No
transcription provider or transient transcript is used to obtain comparisons.
"""

from threading import Event, Thread, current_thread
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.database import create_database_engine, create_session_factory
from app.database_models import MEASUREMENT_VERSION, QuestionAttempt, TranscriptionMeasurement
from app.main import app
from app.session_routes import get_session_service
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService
from app.speaking_metrics import SpeakingMetrics


METRICS = (
    "recognized_word_count", "um_count", "uh_count",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
)
IDENTITY_FIELDS = {
    "id", "attempt_number", "measurement_id", "measurement_version", "measurement_source",
}
METRIC_FIELDS = {
    "before", "after", "delta", "before_unavailable_reason", "after_unavailable_reason",
    "comparable", "comparison_unavailable_reason",
}


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in comparison tests")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("Provider requests are forbidden in comparison tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)


@pytest.fixture
def sessions(postgres_session_factory):
    return InterviewSessionService(postgres_session_factory)


@pytest.fixture
def client(sessions):
    app.dependency_overrides[get_session_service] = lambda: sessions
    try:
        with TestClient(app) as result:
            yield result
    finally:
        app.dependency_overrides.pop(get_session_service, None)


def metrics(**changes):
    values = {
        "recognized_word_count": 4,
        "um_count": 1,
        "uh_count": 2,
        "filler_unavailable_reason": None,
        "timed_utterance_span_seconds": 1.234567890123,
        "estimated_words_per_minute": 194.40000174967392,
        "timing_unavailable_reason": None,
    }
    values.update(changes)
    return SpeakingMetrics(**values)


def submit(sessions, identifier, *, question=0, revision=0, measurement=None, answer="Answer"):
    return sessions.submit_attempt(identifier, question, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer, measurement_id=measurement,
    )).attempt


def measured_attempt(sessions, identifier, values=None, *, question=0, revision=0, answer="Answer"):
    identifier_of_measurement = sessions.create_measurement(
        identifier, question, values or metrics(), expected_last_attempt_number=revision,
    )
    return submit(
        sessions, identifier, question=question, revision=revision,
        measurement=identifier_of_measurement, answer=answer,
    )


def continue_question(sessions, identifier, question=0, revision=1):
    return sessions.continue_question(identifier, question, ContinueRequest(
        expected_last_attempt_number=revision,
    ))


def identity(attempt):
    return {
        "id": str(attempt.id), "attempt_number": attempt.attempt_number,
        "measurement_id": str(attempt.measurement_id) if attempt.measurement_id is not None else None,
        "measurement_version": MEASUREMENT_VERSION if attempt.measurement_id is not None else None,
        "measurement_source": "original_transcription" if attempt.measurement_id is not None else None,
    }


def comparison_url(identifier, question=0):
    return f"/api/sessions/{identifier}/questions/{question}/comparison"


def payload(sessions, identifier, question=0, **selectors):
    return sessions.get_comparison(identifier, question, **selectors).model_dump(mode="json")


def assert_metric(result, name, before, after, before_reason=None, after_reason=None,
                  unavailable=None):
    assert result["comparison"][name] == {
        "before": before, "after": after,
        "delta": after - before if unavailable is None else None,
        "before_unavailable_reason": before_reason,
        "after_unavailable_reason": after_reason,
        "comparable": unavailable is None,
        "comparison_unavailable_reason": unavailable,
    }


def test_zero_attempts_default_is_successful_null_comparison(client, sessions):
    created = sessions.start()
    response = client.get(comparison_url(created.id))
    assert response.status_code == 200
    assert response.json() == {
        "session_id": str(created.id), "question_index": 0,
        "before_attempt": None, "after_attempt": None, "comparison": None,
    }


def test_one_attempt_default_exposes_identity_without_inventing_after(client, sessions):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    response = client.get(comparison_url(created.id))
    assert response.status_code == 200
    assert response.json() == {
        "session_id": str(created.id), "question_index": 0,
        "before_attempt": identity(first), "after_attempt": None, "comparison": None,
    }


def test_two_attempts_default_selects_first_and_latest(client, sessions):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    second = measured_attempt(sessions, created.id, metrics(recognized_word_count=9), revision=1)
    response = client.get(comparison_url(created.id))
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"session_id", "question_index", "before_attempt", "after_attempt", "comparison"}
    assert result["before_attempt"] == identity(first)
    assert result["after_attempt"] == identity(second)
    assert set(result["before_attempt"]) == set(result["after_attempt"]) == IDENTITY_FIELDS
    assert set(result["comparison"]) == set(METRICS)
    assert all(set(metric) == METRIC_FIELDS for metric in result["comparison"].values())
    assert_metric(result, "recognized_word_count", 4, 9)


def test_three_attempts_default_compares_one_and_three(sessions):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    measured_attempt(sessions, created.id, metrics(recognized_word_count=20), revision=1)
    third = measured_attempt(sessions, created.id, metrics(recognized_word_count=7), revision=2)
    result = payload(sessions, created.id)
    assert result["before_attempt"] == identity(first)
    assert result["after_attempt"] == identity(third)
    assert_metric(result, "recognized_word_count", 4, 7)


@pytest.mark.parametrize("selectors, numbers", [
    ({"before": 1, "after": 2}, (1, 2)),
    ({"before": 2, "after": 3}, (2, 3)),
    ({"before": 2}, (2, 3)),
    ({"after": 2}, (1, 2)),
])
def test_explicit_and_partial_selectors_support_later_attempts(client, sessions, selectors, numbers):
    created = sessions.start()
    attempts = [measured_attempt(
        sessions, created.id, metrics(recognized_word_count=number * 10), revision=number - 1,
    ) for number in (1, 2, 3)]
    response = client.get(comparison_url(created.id), params=selectors)
    assert response.status_code == 200
    result = response.json()
    before, after = numbers
    assert result["before_attempt"] == identity(attempts[before - 1])
    assert result["after_attempt"] == identity(attempts[after - 1])
    assert_metric(result, "recognized_word_count", before * 10, after * 10)


@pytest.mark.parametrize("selectors", [
    {"before": 9, "after": 2}, {"before": 1, "after": 9}, {"before": 9}, {"after": 9},
])
def test_missing_explicit_attempt_is_404(client, sessions, selectors):
    created = sessions.start()
    submit(sessions, created.id)
    submit(sessions, created.id, revision=1)
    response = client.get(comparison_url(created.id), params=selectors)
    assert response.status_code == 404
    assert response.json() == {"detail": "Attempt not found."}


@pytest.mark.parametrize("number_of_attempts, selectors", [
    (0, {"after": 1}), (1, {"after": 2}), (1, {"before": 2}),
])
def test_explicit_missing_resource_does_not_fall_back_to_null(client, sessions, number_of_attempts,
                                                            selectors):
    created = sessions.start()
    if number_of_attempts:
        submit(sessions, created.id)
    response = client.get(comparison_url(created.id), params=selectors)
    assert response.status_code == 404
    assert response.json() == {"detail": "Attempt not found."}


@pytest.mark.parametrize("selectors", [
    {"before": 1, "after": 1}, {"before": 2, "after": 1},
    {"before": 2},
])
def test_existing_identical_or_reverse_selectors_are_422(client, sessions, selectors):
    created = sessions.start()
    submit(sessions, created.id)
    submit(sessions, created.id, revision=1)
    response = client.get(comparison_url(created.id), params=selectors)
    assert response.status_code == 422
    assert response.json() == {"detail": "before must be less than after."}


@pytest.mark.parametrize("selectors", [
    {"before": 0}, {"after": -1}, {"before": "one"}, {"after": "1.5"},
    {"before": ""},
])
def test_invalid_selector_shapes_are_422(client, sessions, selectors):
    created = sessions.start()
    response = client.get(comparison_url(created.id), params=selectors)
    assert response.status_code == 422


def test_unknown_session_is_404(client):
    response = client.get(comparison_url(uuid4()))
    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found."}


@pytest.mark.parametrize("question, status", [(-1, 422), (5, 404), ("zero", 422)])
def test_question_must_exist_in_snapshot(client, sessions, question, status):
    created = sessions.start()
    response = client.get(comparison_url(created.id, question))
    assert response.status_code == status
    if status == 404:
        assert response.json() == {"detail": "Question not found."}


def test_each_metric_uses_persisted_values_and_exact_unrounded_delta(sessions):
    created = sessions.start()
    before = metrics()
    after = metrics(
        recognized_word_count=9, um_count=0, uh_count=5,
        timed_utterance_span_seconds=2.987654321987,
        estimated_words_per_minute=180.742938102317,
    )
    measured_attempt(sessions, created.id, before)
    measured_attempt(sessions, created.id, after, revision=1)
    result = payload(sessions, created.id)
    for name in METRICS:
        assert_metric(result, name, getattr(before, name), getattr(after, name))
    assert result["comparison"]["timed_utterance_span_seconds"]["delta"] != round(
        after.timed_utterance_span_seconds - before.timed_utterance_span_seconds, 2,
    )
    assert result["comparison"]["estimated_words_per_minute"]["delta"] != round(
        after.estimated_words_per_minute - before.estimated_words_per_minute, 2,
    )


def test_measured_zero_counts_are_values_and_remain_comparable(sessions):
    created = sessions.start()
    zero = metrics(
        recognized_word_count=0, um_count=0, uh_count=0,
        timed_utterance_span_seconds=None, estimated_words_per_minute=None,
        timing_unavailable_reason="missing_timings",
    )
    measured_attempt(sessions, created.id, zero)
    measured_attempt(sessions, created.id, zero, revision=1)
    result = payload(sessions, created.id)
    for name in ("recognized_word_count", "um_count", "uh_count"):
        assert_metric(result, name, 0, 0)


@pytest.mark.parametrize("before_measured, after_measured, unavailable", [
    (False, True, "before_unavailable"),
    (True, False, "after_unavailable"),
    (False, False, "both_unavailable"),
])
def test_typed_attempts_never_receive_inferred_measurements(sessions, before_measured, after_measured,
                                                         unavailable):
    created = sessions.start()
    # An unlinked recording cannot make a typed attempt measured.
    sessions.create_measurement(created.id, 0, metrics(recognized_word_count=999),
                                expected_last_attempt_number=0)
    before = measured_attempt(sessions, created.id) if before_measured else submit(sessions, created.id)
    after = (measured_attempt(sessions, created.id, revision=1)
             if after_measured else submit(sessions, created.id, revision=1))
    result = payload(sessions, created.id)
    assert result["before_attempt"] == identity(before)
    assert result["after_attempt"] == identity(after)
    for name in METRICS:
        value = getattr(metrics(), name)
        assert_metric(
            result, name, value if before_measured else None, value if after_measured else None,
            None if before_measured else "no_measurement",
            None if after_measured else "no_measurement", unavailable,
        )


@pytest.mark.parametrize("unavailable_side", ["before", "after", "both"])
def test_unsupported_filler_language_preserves_each_side_reason(sessions, unavailable_side):
    created = sessions.start()
    unsupported = metrics(um_count=None, uh_count=None, filler_unavailable_reason="unsupported_language")
    before = unsupported if unavailable_side in ("before", "both") else metrics()
    after = unsupported if unavailable_side in ("after", "both") else metrics()
    measured_attempt(sessions, created.id, before)
    measured_attempt(sessions, created.id, after, revision=1)
    result = payload(sessions, created.id)
    for name in ("um_count", "uh_count"):
        assert_metric(result, name, getattr(before, name), getattr(after, name),
                      before.filler_unavailable_reason, after.filler_unavailable_reason,
                      f"{unavailable_side}_unavailable")
    assert_metric(result, "recognized_word_count", 4, 4)


@pytest.mark.parametrize("reason", [
    "missing_timings", "timing_coverage_mismatch", "invalid_timing",
    "invalid_timing_order", "unusable_span",
])
def test_persisted_timing_unavailability_is_not_recomputed(sessions, reason):
    created = sessions.start()
    unavailable = metrics(timed_utterance_span_seconds=None, estimated_words_per_minute=None,
                          timing_unavailable_reason=reason)
    measured_attempt(sessions, created.id, unavailable)
    measured_attempt(sessions, created.id, revision=1)
    result = payload(sessions, created.id)
    for name in ("timed_utterance_span_seconds", "estimated_words_per_minute"):
        assert_metric(result, name, None, getattr(metrics(), name), reason, None, "before_unavailable")


def test_version_mismatch_preserves_values_but_prevents_every_delta(sessions, postgres_session_factory):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    after = metrics(recognized_word_count=14)
    # Measurements are immutable. Insert a historical definition at creation;
    # do not disable a constraint or update an already persisted measurement.
    with postgres_session_factory.begin() as database:
        row = TranscriptionMeasurement(
            session_id=created.id, question_index=0, measurement_version="speaking-metrics-historical",
            measurement_source=after.source, recognized_word_count=after.recognized_word_count,
            um_count=after.um_count, uh_count=after.uh_count,
            filler_unavailable_reason=after.filler_unavailable_reason,
            timed_utterance_span_seconds=after.timed_utterance_span_seconds,
            estimated_words_per_minute=after.estimated_words_per_minute,
            timing_unavailable_reason=after.timing_unavailable_reason,
        )
        database.add(row)
        database.flush()
        measurement_id = row.id
    second = submit(sessions, created.id, revision=1, measurement=measurement_id)
    result = payload(sessions, created.id)
    assert result["before_attempt"] == identity(first)
    assert result["after_attempt"]["id"] == str(second.id)
    assert result["after_attempt"]["measurement_version"] == "speaking-metrics-historical"
    for name in METRICS:
        assert_metric(result, name, getattr(metrics(), name), getattr(after, name),
                      unavailable="measurement_version_mismatch")


def test_edited_answers_unlinked_recordings_and_other_attempts_cannot_supply_metrics(sessions):
    created = sessions.start()
    first = measured_attempt(sessions, created.id, metrics(recognized_word_count=7),
                             answer="PRIVATE-EDITED-FIRST-TEXT")
    second = measured_attempt(sessions, created.id, metrics(recognized_word_count=11), revision=1,
                              answer="PRIVATE-EDITED-SECOND-TEXT")
    measured_attempt(sessions, created.id, metrics(recognized_word_count=500), revision=2)
    sessions.create_measurement(created.id, 0, metrics(recognized_word_count=999),
                                expected_last_attempt_number=3)
    result = payload(sessions, created.id, before=1, after=2)
    assert result["before_attempt"] == identity(first)
    assert result["after_attempt"] == identity(second)
    assert_metric(result, "recognized_word_count", 7, 11)
    assert "PRIVATE-EDITED" not in str(result)


def test_explicit_selector_scope_cannot_find_other_session_or_question_attempts(client, sessions):
    owner = sessions.start()
    other = sessions.start()
    submit(sessions, owner.id)
    submit(sessions, other.id)
    submit(sessions, other.id, revision=1)
    continue_question(sessions, owner.id)
    submit(sessions, owner.id, question=1)
    submit(sessions, owner.id, question=1, revision=1)
    response = client.get(comparison_url(owner.id, 0), params={"before": 1, "after": 2})
    assert response.status_code == 404
    assert response.json() == {"detail": "Attempt not found."}


def test_current_finalized_and_completed_questions_remain_comparable(sessions):
    created = sessions.start()
    measured_attempt(sessions, created.id)
    measured_attempt(sessions, created.id, metrics(recognized_word_count=8), revision=1)
    expected = payload(sessions, created.id)
    assert sessions.get(created.id).current_question_index == 0
    continue_question(sessions, created.id, revision=2)
    assert payload(sessions, created.id) == expected
    for question in range(1, 5):
        submit(sessions, created.id, question=question)
        continue_question(sessions, created.id, question=question)
    assert sessions.get(created.id).status == "completed"
    assert payload(sessions, created.id) == expected


def test_comparison_survives_service_and_engine_reconstruction(sessions, postgres_engine):
    created = sessions.start()
    measured_attempt(sessions, created.id)
    measured_attempt(sessions, created.id, metrics(recognized_word_count=15), revision=1)
    before = payload(sessions, created.id)
    postgres_engine.dispose()
    engine = create_database_engine(postgres_engine.url)
    rebuilt = InterviewSessionService(create_session_factory(engine))
    try:
        assert payload(rebuilt, created.id) == before
    finally:
        engine.dispose()


def test_comparison_reads_one_snapshot_without_blocking_concurrent_retry(sessions, postgres_engine):
    created = sessions.start()
    first = measured_attempt(sessions, created.id)
    second = measured_attempt(sessions, created.id, metrics(recognized_word_count=10), revision=1)
    reader_executed = Event()
    release_reader = Event()
    writer_finished = Event()
    results = {}
    errors = {}
    reader_statements = []

    def pause_reader(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name != "comparison-snapshot-reader":
            return
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        reader_statements.append(statement)
        assert "FOR UPDATE" not in statement.upper()
        if len(reader_statements) == 1:
            reader_executed.set()
            if not release_reader.wait(10):
                raise AssertionError("Comparison read was not released")

    def read():
        try:
            results["read"] = payload(sessions, created.id)
        except Exception as failure:
            errors["read"] = failure

    def write():
        try:
            results["write"] = measured_attempt(
                sessions, created.id, metrics(recognized_word_count=77), revision=2,
            )
        except Exception as failure:
            errors["write"] = failure
        finally:
            writer_finished.set()

    reader = Thread(name="comparison-snapshot-reader", target=read, daemon=True)
    writer = Thread(name="comparison-snapshot-writer", target=write, daemon=True)
    started = []
    event.listen(postgres_engine, "after_cursor_execute", pause_reader)
    try:
        reader.start()
        started.append(reader)
        assert reader_executed.wait(5), "Comparison statement did not execute"
        writer.start()
        started.append(writer)
        assert writer_finished.wait(3), "Read blocked the same-session retry"
        assert "write" in results
    finally:
        release_reader.set()
        for worker in started:
            worker.join(10)
        event.remove(postgres_engine, "after_cursor_execute", pause_reader)
    assert all(not worker.is_alive() for worker in started)
    assert errors == {}
    assert len(reader_statements) == 1
    assert "question_attempts" in reader_statements[0]
    assert "transcription_measurements" in reader_statements[0]
    assert results["read"]["before_attempt"] == identity(first)
    assert results["read"]["after_attempt"] == identity(second)
    assert_metric(results["read"], "recognized_word_count", 4, 10)
    later = payload(sessions, created.id)
    assert later["after_attempt"] == identity(results["write"])
    assert_metric(later, "recognized_word_count", 4, 77)


def test_comparison_is_read_only_and_leaves_attempts_and_measurements_unchanged(
        sessions, postgres_session_factory):
    created = sessions.start()
    measured_attempt(sessions, created.id)
    measured_attempt(sessions, created.id, revision=1)

    def rows():
        with postgres_session_factory() as database:
            attempts = database.execute(select(QuestionAttempt.__table__).where(
                QuestionAttempt.session_id == created.id,
            ).order_by(QuestionAttempt.attempt_number)).all()
            measurements = database.execute(select(TranscriptionMeasurement.__table__).where(
                TranscriptionMeasurement.session_id == created.id,
            ).order_by(TranscriptionMeasurement.id)).all()
            return attempts, measurements

    before = rows()
    session_before = sessions.get(created.id)
    result = payload(sessions, created.id)
    assert UUID(result["before_attempt"]["measurement_id"]) != UUID(result["after_attempt"]["measurement_id"])
    assert rows() == before
    assert sessions.get(created.id) == session_before

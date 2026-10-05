from collections.abc import Iterator
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session_routes import get_session_service
from app.sessions import InterviewSessionService


@pytest.fixture
def client(postgres_session_factory) -> Iterator[TestClient]:
    service = InterviewSessionService(postgres_session_factory)
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session_service)


def question_url(session_id: str, question_index: int) -> str:
    return f"/api/sessions/{session_id}/questions/{question_index}"


def submit(client: TestClient, session_id: str, question_index: int, answer: str,
           expected_last_attempt_number: int = 0):
    return client.post(
        f"{question_url(session_id, question_index)}/attempts",
        json={"answer": answer, "expected_last_attempt_number": expected_last_attempt_number},
    )


def continue_question(client: TestClient, session_id: str, question_index: int,
                      expected_last_attempt_number: int = 1):
    return client.post(
        f"{question_url(session_id, question_index)}/continue",
        json={"expected_last_attempt_number": expected_last_attempt_number},
    )


def test_create_and_retrieve_session(client: TestClient) -> None:
    response = client.post("/api/sessions")
    assert response.status_code == 201
    session = response.json()
    assert str(UUID(session["id"])) == session["id"]
    assert session["status"] == "active"
    assert session["current_question_index"] == 0
    assert session["current_question"] == "Tell me about yourself."
    assert session["current_question_latest_attempt_number"] == 0
    assert len(session["questions"]) == 5
    assert session["answers"] == []
    retrieved = client.get(response.headers["Location"])
    assert retrieved.status_code == 200
    assert retrieved.json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []


def test_sessions_have_unique_ids_and_independent_answers(client: TestClient) -> None:
    first = client.post("/api/sessions").json()
    second = client.post("/api/sessions").json()
    assert first["id"] != second["id"]
    assert submit(client, first["id"], 0, "One").status_code == 201
    assert continue_question(client, first["id"], 0).status_code == 200
    assert client.get(f"/api/sessions/{second['id']}").json() == second
    assert client.get(f"{question_url(second['id'], 0)}/attempts").json() == []


def test_submission_persists_attempt_without_advancing_question(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    response = submit(client, session["id"], 0, "  My answer.  ")
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"attempt", "session"}
    attempt = payload["attempt"]
    assert set(attempt) == {
        "id", "question_index", "attempt_number", "answer", "submitted_at", "measurement_id",
    }
    assert str(UUID(attempt["id"])) == attempt["id"]
    assert attempt["question_index"] == 0
    assert attempt["attempt_number"] == 1
    assert attempt["answer"] == "My answer."
    assert datetime.fromisoformat(attempt["submitted_at"]).tzinfo is not None
    assert attempt["measurement_id"] is None
    updated = payload["session"]
    assert updated == {**session, "current_question_latest_attempt_number": 1}
    assert client.get(f"/api/sessions/{session['id']}").json() == updated
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == [attempt]


def test_retry_appends_ordered_attempts_and_preserves_first_attempt(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    attempts = []
    for revision, answer in enumerate(["First answer", "Second answer", "Third answer"]):
        response = submit(client, session["id"], 0, answer, revision)
        assert response.status_code == 201
        payload = response.json()
        attempt = payload["attempt"]
        assert attempt["attempt_number"] == revision + 1
        assert attempt["answer"] == answer
        assert attempt["measurement_id"] is None
        attempts.append(attempt)
        assert payload["session"] == {
            **session, "current_question_latest_attempt_number": revision + 1,
        }
        assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == attempts
    assert len({attempt["id"] for attempt in attempts}) == 3
    assert client.get(f"/api/sessions/{session['id']}").json()["answers"] == []
    assert client.get(f"{question_url(session['id'], 1)}/attempts").json() == []


def test_continue_finalizes_latest_attempt_and_advances_question(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    first = submit(client, session["id"], 0, "First answer").json()["attempt"]
    second = submit(client, session["id"], 0, "Second answer", 1).json()["attempt"]
    response = continue_question(client, session["id"], 0, 2)
    assert response.status_code == 200
    updated = response.json()
    assert updated["answers"] == ["Second answer"]
    assert updated["current_question_index"] == 1
    assert updated["current_question"] == "Tell me about a challenging problem you solved."
    assert updated["current_question_latest_attempt_number"] == 0
    assert updated["status"] == "active"
    assert client.get(f"/api/sessions/{session['id']}").json() == updated
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == [first, second]
    assert continue_question(client, session["id"], 0, 2).status_code == 409
    assert client.get(f"/api/sessions/{session['id']}").json() == updated


def test_complete_session_only_after_final_continue_and_reject_extra_writes(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}"
    answers = []
    for index, question in enumerate(session["questions"]):
        answer = f"Answer {index}"
        response = submit(client, session["id"], index, answer)
        assert response.status_code == 201
        pending = response.json()["session"]
        assert pending["answers"] == answers
        assert pending["current_question_index"] == index
        assert pending["current_question"] == question
        assert pending["current_question_latest_attempt_number"] == 1
        assert pending["status"] == "active"
        answers.append(answer)
        response = continue_question(client, session["id"], index)
        assert response.status_code == 200
        updated = response.json()
        assert updated["answers"] == answers
        assert updated["current_question_index"] == index + 1
        assert updated["current_question_latest_attempt_number"] == 0
        if index + 1 < len(session["questions"]):
            assert updated["status"] == "active"
            assert updated["current_question"] == session["questions"][index + 1]
    assert updated["status"] == "completed"
    assert updated["current_question"] is None
    assert client.get(url).json() == updated
    assert submit(client, session["id"], 4, "Extra", 1).status_code == 409
    assert continue_question(client, session["id"], 4, 1).status_code == 409
    assert client.get(url).json() == updated
    assert len(client.get(f"{question_url(session['id'], 4)}/attempts").json()) == 1


def test_continue_without_any_attempt_fails_without_mutation(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    assert continue_question(client, session["id"], 0, 0).status_code == 409
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []


@pytest.mark.parametrize("index", [0, 2])
def test_reject_stale_or_future_question_writes(client: TestClient, index: int) -> None:
    session = client.post("/api/sessions").json()
    assert submit(client, session["id"], 0, "First").status_code == 201
    updated = continue_question(client, session["id"], 0).json()
    assert submit(client, session["id"], index, "Retry", 1).status_code == 409
    assert continue_question(client, session["id"], index, 1).status_code == 409
    assert client.get(f"/api/sessions/{session['id']}").json() == updated


def test_stale_revision_rejects_duplicate_submission_and_continue(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    first_response = submit(client, session["id"], 0, "First")
    assert first_response.status_code == 201
    first = first_response.json()
    response = submit(client, session["id"], 0, "Duplicate", 0)
    assert response.status_code == 409
    assert response.json() == {"detail": "Attempt revision does not match the current question."}
    response = continue_question(client, session["id"], 0, 0)
    assert response.status_code == 409
    assert response.json() == {"detail": "Attempt revision does not match the current question."}
    assert client.get(f"/api/sessions/{session['id']}").json() == first["session"]
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == [first["attempt"]]
    second_response = submit(client, session["id"], 0, "Second", 1)
    assert second_response.status_code == 201
    assert continue_question(client, session["id"], 0, 1).status_code == 409
    assert client.get(f"/api/sessions/{session['id']}").json() == second_response.json()["session"]


def test_future_revision_rejected_before_first_submission(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    response = submit(client, session["id"], 0, "Answer", 1)
    assert response.status_code == 409
    assert response.json() == {"detail": "Attempt revision does not match the current question."}
    response = continue_question(client, session["id"], 0, 1)
    assert response.status_code == 409
    assert response.json() == {"detail": "Current question has no submitted attempts."}
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []


def test_nonexistent_session(client: TestClient) -> None:
    session_id = str(uuid4())
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert submit(client, session_id, 0, "Answer").status_code == 404
    assert continue_question(client, session_id, 0, 0).status_code == 404
    assert client.get(f"{question_url(session_id, 0)}/attempts").status_code == 404


@pytest.mark.parametrize("answer", ["", " \n\t ", None, 123, True, [], {}, "x" * 10001, "a\0b"])
def test_reject_invalid_answer(client: TestClient, answer: object) -> None:
    session = client.post("/api/sessions").json()
    assert client.post(
        f"{question_url(session['id'], 0)}/attempts",
        json={"expected_last_attempt_number": 0, "answer": answer},
    ).status_code == 422
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []


def test_accept_maximum_length_answer(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    response = submit(client, session["id"], 0, "x" * 10000)
    assert response.status_code == 201
    assert response.json()["attempt"]["answer"] == "x" * 10000


@pytest.mark.parametrize("payload", [
    {}, {"expected_last_attempt_number": 0}, {"answer": "Hi"},
    {"expected_last_attempt_number": 0, "answer": "Hi", "extra": 1},
    {"expected_last_attempt_number": 0, "answer": "Hi", "attempt_number": 1},
    {"expected_last_attempt_number": 0, "answer": "Hi", "question_index": 0},
    {"expected_last_attempt_number": 0, "answer": "Hi", "id": str(uuid4())},
    {"expected_last_attempt_number": 0, "answer": "Hi", "measurement_id": "not-a-uuid"},
])
def test_reject_invalid_attempt_request(client: TestClient, payload: dict[str, object]) -> None:
    session = client.post("/api/sessions").json()
    assert client.post(f"{question_url(session['id'], 0)}/attempts", json=payload).status_code == 422
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []


@pytest.mark.parametrize("revision", [-1, True, False, "0", 0.0, None, [], {}])
@pytest.mark.parametrize("operation", ["attempts", "continue"])
def test_reject_non_strict_or_negative_revision(client: TestClient, revision: object,
                                               operation: str) -> None:
    session = client.post("/api/sessions").json()
    payload = {"expected_last_attempt_number": revision}
    if operation == "attempts":
        payload["answer"] = "Hi"
    assert client.post(f"{question_url(session['id'], 0)}/{operation}", json=payload).status_code == 422
    assert client.get(f"/api/sessions/{session['id']}").json() == session


@pytest.mark.parametrize("payload", [
    {}, {"expected_last_attempt_number": 0, "extra": 1},
    {"expected_last_attempt_number": 0, "answer": "Hi"},
    {"expected_last_attempt_number": 0, "question_index": 0},
])
def test_reject_invalid_continue_request(client: TestClient, payload: dict[str, object]) -> None:
    session = client.post("/api/sessions").json()
    assert client.post(f"{question_url(session['id'], 0)}/continue", json=payload).status_code == 422
    assert client.get(f"/api/sessions/{session['id']}").json() == session


@pytest.mark.parametrize("index", ["-1", "not-an-index", "1.5"])
def test_reject_invalid_question_path(client: TestClient, index: str) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}/questions/{index}"
    assert client.get(f"{url}/attempts").status_code == 422
    assert client.post(f"{url}/attempts", json={
        "expected_last_attempt_number": 0, "answer": "Hi",
    }).status_code == 422
    assert client.post(f"{url}/continue", json={"expected_last_attempt_number": 0}).status_code == 422
    assert client.get(f"/api/sessions/{session['id']}").json() == session


@pytest.mark.parametrize("index", [5, 100])
def test_attempt_retrieval_rejects_question_outside_snapshot(client: TestClient, index: int) -> None:
    session = client.post("/api/sessions").json()
    assert client.get(f"{question_url(session['id'], index)}/attempts").status_code == 404
    assert client.get(f"/api/sessions/{session['id']}").json() == session


def test_reject_malformed_session_id(client: TestClient) -> None:
    assert client.get("/api/sessions/not-a-uuid").status_code == 422
    url = "/api/sessions/not-a-uuid/questions/0"
    assert client.get(f"{url}/attempts").status_code == 422
    assert client.post(f"{url}/attempts", json={
        "expected_last_attempt_number": 0, "answer": "Hi",
    }).status_code == 422
    assert client.post(f"{url}/continue", json={"expected_last_attempt_number": 0}).status_code == 422


def test_legacy_answers_route_cannot_bypass_attempt_lifecycle(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    assert client.post(
        f"/api/sessions/{session['id']}/answers", json={"question_index": 0, "answer": "Answer"},
    ).status_code == 404
    assert client.get(f"/api/sessions/{session['id']}").json() == session
    assert client.get(f"{question_url(session['id'], 0)}/attempts").json() == []

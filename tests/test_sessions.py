from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session_routes import get_session_service
from app.sessions import InterviewSessionService
from conftest import answer_payload


@pytest.fixture
def client() -> Iterator[TestClient]:
    service = InterviewSessionService()
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session_service)


def test_create_and_retrieve_session(client: TestClient) -> None:
    response = client.post("/api/sessions")
    assert response.status_code == 201
    session = response.json()
    assert str(UUID(session["id"])) == session["id"]
    assert session["status"] == "active"
    assert session["current_question_index"] == 0
    assert session["current_question"] == "Tell me about yourself."
    assert len(session["questions"]) == 5
    assert session["answers"] == []
    retrieved = client.get(response.headers["Location"])
    assert retrieved.status_code == 200
    assert retrieved.json() == session


def test_sessions_have_unique_ids_and_independent_answers(client: TestClient) -> None:
    first = client.post("/api/sessions").json()
    second = client.post("/api/sessions").json()
    assert first["id"] != second["id"]
    client.post(f"/api/sessions/{first['id']}/answers", json=answer_payload(0, "One"))
    assert client.get(f"/api/sessions/{second['id']}").json() == second


def test_answer_advances_question(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    response = client.post(
        f"/api/sessions/{session['id']}/answers",
        json=answer_payload(0, "  My answer.  "),
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["answers"] == ["My answer."]
    assert updated["current_question_index"] == 1
    assert updated["current_question"] == "Tell me about a challenging problem you solved."
    assert updated["status"] == "active"
    assert client.get(f"/api/sessions/{session['id']}").json() == updated


def test_complete_session_and_reject_extra_answer(client: TestClient) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}"
    answers = []
    for index, _question in enumerate(session["questions"]):
        answers.append(f"Answer {index}")
        response = client.post(f"{url}/answers", json=answer_payload(index, answers[-1]))
        assert response.status_code == 200
        updated = response.json()
        assert updated["answers"] == answers
        assert updated["current_question_index"] == index + 1
        if index + 1 < len(session["questions"]):
            assert updated["status"] == "active"
            assert updated["current_question"] == session["questions"][index + 1]
    assert updated["status"] == "completed"
    assert updated["current_question"] is None
    assert client.get(url).json() == updated
    assert client.post(f"{url}/answers", json=answer_payload(5, "Extra")).status_code == 409
    assert client.get(url).json() == updated


@pytest.mark.parametrize("index", [0, 2])
def test_reject_stale_or_future_answer(client: TestClient, index: int) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}"
    updated = client.post(f"{url}/answers", json=answer_payload(0, "First")).json()
    assert client.post(f"{url}/answers", json=answer_payload(index, "Retry")).status_code == 409
    assert client.get(url).json() == updated


def test_nonexistent_session(client: TestClient) -> None:
    url = f"/api/sessions/{uuid4()}"
    assert client.get(url).status_code == 404
    assert client.post(f"{url}/answers", json=answer_payload()).status_code == 404


@pytest.mark.parametrize("answer", ["", " \n\t ", None, 123, True, [], {}, "x" * 10001])
def test_reject_invalid_answer(client: TestClient, answer: object) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}"
    assert client.post(f"{url}/answers", json=answer_payload(0, answer)).status_code == 422
    assert client.get(url).json() == session


@pytest.mark.parametrize("payload", [{}, {"question_index": 0}, {"answer": "Hi"},
    {"question_index": -1, "answer": "Hi"}, {"question_index": True, "answer": "Hi"},
    {"question_index": "0", "answer": "Hi"}, {"question_index": 0, "answer": "Hi", "extra": 1}])
def test_reject_invalid_request(client: TestClient, payload: dict[str, object]) -> None:
    session = client.post("/api/sessions").json()
    url = f"/api/sessions/{session['id']}"
    identity = {k: v for k, v in answer_payload().items() if k not in ("answer", "question_index")}
    assert client.post(f"{url}/answers", json={**identity, **payload}).status_code == 422
    assert client.get(url).json() == session


def test_reject_malformed_session_id(client: TestClient) -> None:
    assert client.get("/api/sessions/not-a-uuid").status_code == 422

"""Offline test support; no provider credentials/network are used in CI."""
from uuid import uuid4

import httpx
import pytest

from app.main import app
from app.nemotron import get_reasoning_service
from app.reasoning import Decision
from app.sessions import AnswerRequest

MOVE_ON = Decision(action='MOVE_ON', reason='Complete for this test.', next_prompt=None)


class MoveOnReasoner:
    async def decide(self, context):
        return MOVE_ON


@pytest.fixture(autouse=True)
def offline_reasoning(monkeypatch):
    monkeypatch.delenv('NVIDIA_API_KEY', raising=False)

    async def blocked(*args, **kwargs):
        raise AssertionError('Real provider network is forbidden in tests')

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', blocked)
    app.dependency_overrides[get_reasoning_service] = lambda: MoveOnReasoner()
    yield
    app.dependency_overrides.pop(get_reasoning_service, None)


def answer_payload(index=0, answer='Answer', revision=None):
    return {'question_index': index, 'turn_revision': index if revision is None else revision,
            'submission_id': str(uuid4()), 'answer': answer}


def advance(service, session_id, index, answer):
    request = AnswerRequest(**answer_payload(index, answer))
    service.begin_submission(session_id, request)
    return service.submit_answer(session_id, request, MOVE_ON)

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.interview_orchestration import submit_with_reasoning
from app.nemotron import get_reasoning_service
from app.reasoning import Decision, InvalidDecision, ReasoningFailed, ReasoningTimeout, ReasoningUnavailable
from app.session_routes import get_session_service
from app.sessions import AnswerRequest, InterviewSessionService, SessionConflict
from conftest import MOVE_ON, answer_payload


class FakeReasoner:
    def __init__(self, decision=MOVE_ON, error=None):
        self.decision, self.error, self.contexts = decision, error, []

    async def decide(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.decision


def request(session, text='Candidate answer', submission_id=None):
    return AnswerRequest(**{**answer_payload(session.current_question_index, text, session.turn_revision),
                            'submission_id': submission_id or uuid4()})


@pytest.mark.parametrize('action', ['FOLLOW_UP', 'CLARIFY', 'CHALLENGE', 'MOVE_ON'])
def test_actions_preserve_separate_turns_and_original_answer(action):
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner(Decision(action=action, reason='Short explanation.',
                                next_prompt=None if action == 'MOVE_ON' else 'Explain your contribution?'))
    updated = asyncio.run(submit_with_reasoning(service, session.id, request(session), fake))
    assert updated.turn_revision == 1
    assert updated.current_question_index == (1 if action == 'MOVE_ON' else 0)
    assert updated.current_prompt == (updated.questions[1] if action == 'MOVE_ON' else 'Explain your contribution?')
    assert updated.answers == ['Candidate answer']
    assert updated.turns[0].prompt == session.current_prompt
    assert updated.turns[0].action == action
    assert 'reason' not in updated.model_dump_json()
    assert fake.contexts[0].question == session.questions[0]
    assert not hasattr(fake.contexts[0], 'id')


def test_probe_cap_skips_provider_and_eventually_completes():
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner(Decision(action='FOLLOW_UP', reason='Detail missing.', next_prompt='What happened?'))
    for index in range(5):
        for probe in range(3):
            session = asyncio.run(submit_with_reasoning(service, session.id, request(session, f'{index}/{probe}'), fake))
        assert session.turns[-1].transition_source == 'probe_limit'
        assert session.turns[-1].action is None
    assert len(fake.contexts) == 10
    assert len(session.turns) == 15
    assert session.answers == [f'{i}/0' for i in range(5)]
    assert session.status == 'completed'
    assert session.current_prompt is None
    assert session.turn_revision == 15
    assert fake.contexts[1].prior_turns[0].answer == '0/0'


@pytest.mark.parametrize('error,status', [(ReasoningUnavailable(), 503), (ReasoningTimeout(), 504),
    (ReasoningFailed('sensitive'), 502), (InvalidDecision('sensitive'), 502)])
def test_errors_leave_state_unchanged_and_allow_retry(error, status):
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner(error=error)
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_reasoning_service] = lambda: fake
    try:
        with TestClient(app) as client:
            payload = request(session).model_dump(mode='json')
            response = client.post(f'/api/sessions/{session.id}/answers', json=payload)
            assert response.status_code == status
            assert 'sensitive' not in response.text
            assert service.get(session.id) == session
            fake.error = None
            assert client.post(f'/api/sessions/{session.id}/answers', json=payload).status_code == 200
    finally:
        app.dependency_overrides.pop(get_session_service)


def test_idempotency_replays_current_state_and_rejects_changed_payload():
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner()
    original = request(session)
    first = asyncio.run(submit_with_reasoning(service, session.id, original, fake))
    second = asyncio.run(submit_with_reasoning(service, session.id, request(first), fake))
    assert asyncio.run(submit_with_reasoning(service, session.id, original, fake)) == second
    assert len(fake.contexts) == 2
    with pytest.raises(SessionConflict):
        asyncio.run(submit_with_reasoning(service, session.id, original.model_copy(update={'answer': 'Changed'}), fake))
    with pytest.raises(SessionConflict):
        asyncio.run(submit_with_reasoning(service, session.id, request(session), fake))
    assert len(fake.contexts) == 2


def test_pending_submission_does_not_hold_lock_or_allow_second_provider_call():
    async def scenario():
        service = InterviewSessionService()
        session = service.start()
        entered, release = asyncio.Event(), asyncio.Event()

        class Waiting:
            async def decide(self, context):
                entered.set()
                await release.wait()
                return MOVE_ON

        original = request(session)
        task = asyncio.create_task(submit_with_reasoning(service, session.id, original, Waiting()))
        await entered.wait()
        assert service.get(session.id) == session
        # A different session remains writable while this provider is pending.
        other = service.start()
        await submit_with_reasoning(service, other.id, request(other), FakeReasoner())
        for competing in [original, request(session)]:
            with pytest.raises(SessionConflict):
                await submit_with_reasoning(service, session.id, competing, FakeReasoner())
        release.set()
        result = await task
        assert len(result.turns) == 1
        assert await submit_with_reasoning(service, session.id, original, FakeReasoner()) == result
    asyncio.run(scenario())


def test_cancellation_releases_reservation():
    async def scenario():
        service = InterviewSessionService()
        session = service.start()
        entered = asyncio.Event()
        class Waiting:
            async def decide(self, context):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(submit_with_reasoning(service, session.id, request(session), Waiting()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.get(session.id) == session
        assert (await submit_with_reasoning(service, session.id, request(session), FakeReasoner())).turn_revision == 1
    asyncio.run(scenario())


def test_invalid_fake_decision_cannot_mutate_engine():
    service = InterviewSessionService()
    session = service.start()
    with pytest.raises(InvalidDecision):
        asyncio.run(submit_with_reasoning(service, session.id, request(session),
            FakeReasoner(Decision.model_construct(action='CHAT', reason='x', next_prompt=None))))
    assert service.get(session.id) == session


def test_same_question_old_turn_rejected_before_provider():
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner(Decision(action='CLARIFY', reason='Ambiguous.', next_prompt='Which project?'))
    asyncio.run(submit_with_reasoning(service, session.id, request(session), fake))
    with pytest.raises(SessionConflict):
        asyncio.run(submit_with_reasoning(service, session.id, request(session), fake))
    with pytest.raises(SessionConflict):
        service.validate_current_turn(session.id, 0, 0)
    assert len(fake.contexts) == 1


def test_orchestration_deadline_releases_pending_turn(monkeypatch):
    import app.interview_orchestration as module
    monkeypatch.setattr(module, 'REASONING_DEADLINE_SECONDS', .01)
    class Waiting:
        async def decide(self, context):
            await asyncio.Event().wait()
    service = InterviewSessionService()
    session = service.start()
    with pytest.raises(ReasoningTimeout):
        asyncio.run(submit_with_reasoning(service, session.id, request(session), Waiting()))
    assert service.get(session.id) == session
    asyncio.run(submit_with_reasoning(service, session.id, request(session), FakeReasoner()))


def test_engine_rechecks_turn_after_late_provider_result():
    service = InterviewSessionService()
    session = service.start()
    original = request(session)
    class Superseded:
        async def decide(self, context):
            # Simulate another engine caller superseding a cancelled reservation.
            service.cancel_submission(session.id, original)
            await submit_with_reasoning(service, session.id, request(session, 'Other answer'), FakeReasoner())
            return MOVE_ON
    with pytest.raises(SessionConflict):
        asyncio.run(submit_with_reasoning(service, session.id, original, Superseded()))
    assert service.get(session.id).answers == ['Other answer']


@pytest.mark.parametrize('field,value', [('turn_revision', None), ('turn_revision', True),
    ('turn_revision', -1), ('turn_revision', '0'), ('submission_id', 'invalid'), ('submission_id', None)])
def test_invalid_turn_contract_never_calls_provider(field, value):
    fake = FakeReasoner()
    app.dependency_overrides[get_reasoning_service] = lambda: fake
    with TestClient(app) as client:
        session = client.post('/api/sessions').json()
        body = {**answer_payload(), field: value}
        assert client.post(f"/api/sessions/{session['id']}/answers", json=body).status_code == 422
        assert fake.contexts == []


@pytest.mark.parametrize('endpoint', ['audio', 'transcriptions'])
def test_same_question_stale_media_rejected(endpoint):
    service = InterviewSessionService()
    session = service.start()
    fake = FakeReasoner(Decision(action='FOLLOW_UP', reason='Incomplete.', next_prompt='What happened next?'))
    asyncio.run(submit_with_reasoning(service, session.id, request(session), fake))
    app.dependency_overrides[get_session_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.post(f'/api/sessions/{session.id}/{endpoint}',
                data={'question_index': '0', 'turn_revision': '0'},
                files={'audio': ('answer.webm', b'fake audio', 'audio/webm')})
            assert response.status_code == 409
            assert service.get(session.id).turn_revision == 1
    finally:
        app.dependency_overrides.pop(get_session_service)


@pytest.mark.parametrize('revision', [None, '-1', 'x', '1.5', '99999999999'])
def test_media_requires_valid_revision(revision):
    with TestClient(app) as client:
        session = client.post('/api/sessions').json()
        data = {'question_index': '0'}
        if revision is not None:
            data['turn_revision'] = revision
        response = client.post(f"/api/sessions/{session['id']}/audio", data=data,
            files={'audio': ('answer.webm', b'fake audio', 'audio/webm')})
        assert response.status_code == 422


def test_snapshot_and_context_cannot_mutate_session():
    service = InterviewSessionService()
    session = service.start()
    answer = request(session)
    snapshot, _ = service.begin_submission(session.id, answer)
    context = service.reasoning_context(snapshot, answer)
    snapshot.questions.clear()
    snapshot.answers.append('Not committed')
    assert service.get(session.id) == session
    with pytest.raises(ValueError):
        context.answer = 'Changed'
    service.cancel_submission(session.id, answer)


def test_committed_retry_after_completion_is_a_read():
    service = InterviewSessionService()
    session = service.start()
    original = request(session)
    fake = FakeReasoner()
    current = asyncio.run(submit_with_reasoning(service, session.id, original, fake))
    for _ in range(4):
        current = asyncio.run(submit_with_reasoning(service, session.id, request(current), fake))
    assert current.status == 'completed'
    assert asyncio.run(submit_with_reasoning(service, session.id, original, fake)) == current
    assert len(fake.contexts) == 5


@pytest.mark.parametrize('field', ['turn_revision', 'submission_id'])
def test_missing_turn_fields_rejected(field):
    with TestClient(app) as client:
        session = client.post('/api/sessions').json()
        body = answer_payload()
        del body[field]
        assert client.post(f"/api/sessions/{session['id']}/answers", json=body).status_code == 422

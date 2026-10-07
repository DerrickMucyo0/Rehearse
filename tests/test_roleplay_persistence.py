"""Adaptive context and atomic advancement against isolated real PostgreSQL."""
import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.database import create_database_engine, create_session_factory
from app.database_models import QuestionAttempt, StoredInterviewSession, User
from app.history import HistoryReadService
from app.roleplay import RoleplayQuestion
from app.scenarios import questions_for_scenario
from app.sessions import AttemptRequest, ContinueRequest, InterviewSessionService, SessionConflict, SessionNotFound

SCENARIOS = ("job_interview", "public_speaking", "thesis_defense", "salary_negotiation")
STALE = "Interview state changed. Recheck before continuing."


@pytest.fixture
def sessions(postgres_session_factory, authenticated_principal):
    return InterviewSessionService(postgres_session_factory, authenticated_principal)


def submit(sessions, identifier, index=0, revision=0, answer="Submitted answer."):
    return sessions.submit_attempt(identifier, index, AttemptRequest(
        expected_last_attempt_number=revision, answer=answer,
    ))


def prepare(sessions, identifier, index=0, revision=1):
    return sessions.prepare_continue(identifier, index, ContinueRequest(expected_last_attempt_number=revision))


def question(number):
    return RoleplayQuestion(
        roleplay_version="live-ai-roleplay-v1",
        next_question=f"What would you do differently in situation {number}?",
    )


def guard(database):
    # Trusted direct-service fixture has an owner but deliberately no real login.
    # HTTP tests separately exercise the production transaction-aware auth guard.
    assert database.in_transaction()


def advance(sessions, identifier, index=0, revision=1):
    snapshot = prepare(sessions, identifier, index, revision)
    return sessions.commit_continue(
        snapshot, question(index + 2) if snapshot.requires_generation else None, principal_guard=guard,
    )


def state(factory, identifier):
    with factory() as database:
        root = dict(database.execute(select(StoredInterviewSession.__table__).where(
            StoredInterviewSession.id == identifier,
        )).mappings().one())
        attempts = tuple(dict(row) for row in database.execute(select(QuestionAttempt.__table__).where(
            QuestionAttempt.session_id == identifier,
        ).order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)).mappings())
        return root, attempts


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_adaptive_prefix_and_latest_answers_are_durable_for_all_scenarios(
        sessions, postgres_session_factory, authenticated_principal, scenario):
    created = sessions.start_adaptive(scenario)
    history = HistoryReadService(postgres_session_factory, authenticated_principal)
    assert created.question_engine == "live-ai-roleplay-v1"
    assert created.questions == list(questions_for_scenario(scenario)[:1])
    assert created.total_questions == 5
    finalized = []
    for index in range(5):
        submit(sessions, created.id, index, answer=f"First answer {index + 1}.")
        submitted = submit(sessions, created.id, index, 1, f"Latest answer {index + 1}.")
        assert submitted.session.answers == finalized
        assert submitted.session.current_question_index == index
        snapshot = prepare(sessions, created.id, index, 2)
        assert tuple(attempt.answer for attempt in snapshot.attempts) == tuple(
            finalized + [f"Latest answer {index + 1}."]
        )
        if index < 4:
            context = snapshot.context
            assert context is not None
            assert context.next_question_number == index + 2
            assert context.scenario_type == scenario
            assert [turn.question_number for turn in context.turns] == list(range(1, index + 2))
            assert [turn.answer for turn in context.turns] == finalized + [f"Latest answer {index + 1}."]
            assert set(context.model_dump()) == {"context_version", "scenario_type", "next_question_number", "turns"}
            assert all(set(turn.model_dump()) == {"question_number", "question", "answer"} for turn in context.turns)
        else:
            assert snapshot.context is None
            assert not snapshot.requires_generation
        updated = sessions.commit_continue(
            snapshot, question(index + 2) if snapshot.requires_generation else None, principal_guard=guard,
        )
        finalized.append(f"Latest answer {index + 1}.")
        assert updated.answers == finalized
        assert updated.current_question_index == index + 1
        assert updated.total_questions == 5
        assert updated.questions[:len(submitted.session.questions)] == submitted.session.questions
        detail = history.get_detail(created.id, question_index=index)
        assert detail.summary.question_engine == "live-ai-roleplay-v1"
        assert detail.summary.total_questions == 5
        assert detail.summary.finalized_question_count == index + 1
        assert len(detail.questions) == min(index + 2, 5)
        assert detail.selected_question is not None
        assert [attempt.is_final for attempt in detail.selected_question.attempts] == [False, True]
    assert updated.status == "completed"
    assert updated.current_question is None
    assert len(updated.questions) == 5
    assert history.get_detail(created.id).summary.completed_at is not None
    with pytest.raises(SessionConflict):
        prepare(sessions, created.id, 4, 2)


def test_service_and_engine_reconstruction_keeps_generated_question_text(
        sessions, postgres_engine, postgres_session_factory, authenticated_principal):
    created = sessions.start_adaptive("thesis_defense")
    submit(sessions, created.id)
    updated = advance(sessions, created.id)
    before = state(postgres_session_factory, created.id)
    rebuilt_engine = create_database_engine(postgres_engine.url)
    try:
        rebuilt_factory = create_session_factory(rebuilt_engine)
        rebuilt = InterviewSessionService(rebuilt_factory, authenticated_principal)
        assert rebuilt.get(created.id) == updated
        assert state(rebuilt_factory, created.id) == before
        detail = HistoryReadService(rebuilt_factory, authenticated_principal).get_detail(created.id)
        assert [overview.question_text for overview in detail.questions] == updated.questions
    finally:
        rebuilt_engine.dispose()


def test_ungenerated_question_reads_return_not_found_without_placeholders(
        sessions, postgres_session_factory, authenticated_principal):
    created = sessions.start_adaptive()
    history = HistoryReadService(postgres_session_factory, authenticated_principal)
    detail = history.get_detail(created.id)
    assert len(detail.questions) == 1
    assert detail.summary.total_questions == 5
    for operation in (
        lambda: history.get_detail(created.id, question_index=1),
        lambda: sessions.get_attempts(created.id, 1),
        lambda: sessions.get_comparison(created.id, 1),
        lambda: sessions.get_diagnosis_context(created.id, 1, 1),
    ):
        with pytest.raises(SessionNotFound):
            operation()


def test_prepare_validates_submission_revision_and_returns_frozen_facts(sessions, postgres_session_factory):
    created = sessions.start_adaptive()
    before = state(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="no submitted attempts"):
        prepare(sessions, created.id, revision=0)
    assert state(postgres_session_factory, created.id) == before
    submit(sessions, created.id)
    with pytest.raises(SessionConflict, match="revision"):
        prepare(sessions, created.id, revision=0)
    snapshot = prepare(sessions, created.id)
    with pytest.raises(FrozenInstanceError):
        snapshot.current_question_index = 1
    with pytest.raises(FrozenInstanceError):
        snapshot.attempts[0].answer = "Changed"


@pytest.mark.parametrize("mutation", ["retry", "continue", "scenario", "prior_answer"])
def test_commit_rechecks_whole_context_and_rejects_stale_candidates(
        sessions, postgres_session_factory, mutation):
    created = sessions.start_adaptive()
    submit(sessions, created.id)
    if mutation == "prior_answer":
        advance(sessions, created.id)
        submit(sessions, created.id, 1)
        snapshot = prepare(sessions, created.id, 1)
        with postgres_session_factory.begin() as database:
            database.execute(update(QuestionAttempt).where(
                QuestionAttempt.session_id == created.id, QuestionAttempt.question_index == 0,
            ).values(answer_text="Changed persisted prior answer."))
    else:
        snapshot = prepare(sessions, created.id)
        if mutation == "retry":
            submit(sessions, created.id, revision=1, answer="Retry committed during inference.")
        elif mutation == "continue":
            advance(sessions, created.id)
        else:
            with postgres_session_factory.begin() as database:
                database.execute(update(StoredInterviewSession).where(
                    StoredInterviewSession.id == created.id,
                ).values(scenario_type="public_speaking"))
    before = state(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match=STALE):
        sessions.commit_continue(snapshot, question(snapshot.current_question_index + 2), principal_guard=guard)
    assert state(postgres_session_factory, created.id) == before


def test_foreign_owner_cannot_prepare_or_commit_another_users_snapshot(
        sessions, postgres_session_factory):
    created = sessions.start_adaptive()
    submit(sessions, created.id)
    snapshot = prepare(sessions, created.id)
    with postgres_session_factory.begin() as database:
        user = User(auth_provider="https://roleplay.example.test", provider_subject=str(uuid4()))
        database.add(user)
        database.flush()
        foreign = AuthenticatedPrincipal(user_id=user.id, auth_session_id=uuid4(), request_context="foreign-context")
    other = InterviewSessionService(postgres_session_factory, foreign)
    before = state(postgres_session_factory, created.id)
    with pytest.raises(SessionNotFound):
        prepare(other, created.id)
    with pytest.raises(SessionNotFound):
        other.commit_continue(snapshot, question(2), principal_guard=guard)
    assert state(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("failure_stage", ["after_flush_postexec", "before_commit"])
@pytest.mark.parametrize("index", [0, 4])
def test_adaptive_append_and_finalization_roll_back_together(
        sessions, postgres_engine, postgres_session_factory, authenticated_principal, failure_stage, index):
    created = sessions.start_adaptive()
    for current in range(index):
        submit(sessions, created.id, current)
        advance(sessions, created.id, current)
    submit(sessions, created.id, index)
    snapshot = prepare(sessions, created.id, index)
    before = state(postgres_session_factory, created.id)

    class FailingSession(Session):
        pass

    def fail(*args):
        raise RuntimeError("Injected adaptive transaction failure")

    event.listen(FailingSession, failure_stage, fail)
    failing = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=FailingSession), authenticated_principal)
    try:
        with pytest.raises(RuntimeError, match="Injected adaptive transaction failure"):
            failing.commit_continue(snapshot, question(index + 2) if index < 4 else None, principal_guard=guard)
    finally:
        event.remove(FailingSession, failure_stage, fail)
    assert state(postgres_session_factory, created.id) == before


@pytest.mark.parametrize("provider_failure", [None, "failed", "cancelled"])
def test_fake_inference_has_no_open_transaction_and_failures_do_not_advance(
        postgres_engine, postgres_session_factory, authenticated_principal, provider_failure):
    databases = []

    class TrackingSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            databases.append(self)

    sessions = InterviewSessionService(sessionmaker(bind=postgres_engine, class_=TrackingSession), authenticated_principal)
    created = sessions.start_adaptive()
    submit(sessions, created.id)
    snapshot = prepare(sessions, created.id)
    before = state(postgres_session_factory, created.id)

    class FakeProvider:
        async def generate(self, context):
            assert context is snapshot.context or context == snapshot.context
            assert databases and all(not database.in_transaction() for database in databases)
            if provider_failure == "failed":
                raise RuntimeError("Fake provider failed")
            if provider_failure == "cancelled":
                raise asyncio.CancelledError()
            return question(2)

    if provider_failure is None:
        proposal = asyncio.run(FakeProvider().generate(snapshot.context))
        assert state(postgres_session_factory, created.id) == before
        updated = sessions.commit_continue(snapshot, proposal, principal_guard=guard)
        assert updated.current_question_index == 1
        assert updated.answers == ["Submitted answer."]
    else:
        expected = RuntimeError if provider_failure == "failed" else asyncio.CancelledError
        with pytest.raises(expected):
            asyncio.run(FakeProvider().generate(snapshot.context))
        assert state(postgres_session_factory, created.id) == before


def test_guard_failure_and_invalid_or_missing_question_cannot_publish(sessions, postgres_session_factory):
    created = sessions.start_adaptive()
    submit(sessions, created.id)
    snapshot = prepare(sessions, created.id)
    before = state(postgres_session_factory, created.id)
    with pytest.raises(TypeError, match="commit guard"):
        sessions.commit_continue(snapshot, question(2))
    with pytest.raises(ValueError, match="validated next question"):
        sessions.commit_continue(snapshot, None, principal_guard=guard)
    invalid = RoleplayQuestion.model_construct(roleplay_version="live-ai-roleplay-v1", next_question="Malformed\nquestion")
    with pytest.raises(ValueError):
        sessions.commit_continue(snapshot, invalid, principal_guard=guard)

    def denied(database):
        assert database.in_transaction()
        raise RuntimeError("Authentication guard rejected commit")

    with pytest.raises(RuntimeError, match="guard rejected"):
        sessions.commit_continue(snapshot, question(2), principal_guard=denied)
    assert state(postgres_session_factory, created.id) == before


def test_legacy_service_and_prepared_continue_never_require_generation(sessions):
    created = sessions.start("public_speaking")
    assert created.question_engine == "deterministic-v1"
    assert len(created.questions) == 5
    submit(sessions, created.id)
    snapshot = prepare(sessions, created.id)
    assert not snapshot.requires_generation
    assert snapshot.context is None
    def forbidden_guard(database):
        raise AssertionError("Legacy Continue must retain its authentication boundary.")

    updated = sessions.commit_continue(snapshot, None, principal_guard=forbidden_guard)
    assert updated.questions == created.questions
    assert updated.answers == ["Submitted answer."]
    submit(sessions, created.id, 1)
    continued = sessions.continue_question(created.id, 1, ContinueRequest(expected_last_attempt_number=1))
    assert continued.current_question_index == 2


def test_direct_legacy_continue_cannot_accidentally_complete_adaptive_q1(sessions, postgres_session_factory):
    created = sessions.start_adaptive()
    submit(sessions, created.id)
    before = state(postgres_session_factory, created.id)
    with pytest.raises(SessionConflict, match="guarded Continue"):
        sessions.continue_question(created.id, 0, ContinueRequest(expected_last_attempt_number=1))
    assert state(postgres_session_factory, created.id) == before

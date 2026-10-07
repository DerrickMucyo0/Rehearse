"""Continue orchestration: closed database work surrounds one roleplay call."""
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.roleplay import RoleplayAdapter, request_roleplay_question
from app.sessions import ContinueRequest, InterviewSession, InterviewSessionService


async def continue_application_attempt(
    sessions: InterviewSessionService,
    adapter: RoleplayAdapter,
    *,
    session_id: UUID,
    question_index: int,
    request: ContinueRequest,
    principal_guard: Callable[[Session], None],
) -> InterviewSession:
    snapshot = await run_in_threadpool(
        sessions.prepare_continue, session_id, question_index, request,
    )
    question = None
    if snapshot.requires_generation:
        question = await request_roleplay_question(adapter, snapshot.context)
    return await run_in_threadpool(
        sessions.commit_continue, snapshot, question, principal_guard=principal_guard,
    )

"""Known-session history reads, without account ownership or enumeration."""
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.database import DatabaseConfigurationError, create_database_engine, create_session_factory
from app.history import (
    HistoryBatchRequest, HistoryDetail, HistoryDetailQuery, HistoryIntegrityError,
    HistoryReadService, HistorySummaries,
)
from app.sessions import SessionNotFound


class HistoryRoute(APIRoute):
    """Scope sanitization and no-store to these new read-only endpoints."""

    async def handle(self, scope, receive, send):
        # Method rejection precedes endpoint handling, so give it the same cache
        # protection without changing any other application routes.
        if self.methods and scope["method"] not in self.methods:
            response = JSONResponse(
                status_code=405, content={"detail": "Method Not Allowed"},
                headers={"Allow": ", ".join(sorted(self.methods)), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request):
            try:
                response = await original(request)
            except RequestValidationError:
                # Normal request validation status, without echoing supplied values.
                response = JSONResponse(status_code=422, content={"detail": "Invalid history request."})
            except SessionNotFound:
                response = JSONResponse(status_code=404, content={"detail": "Session or question not found."})
            except (HistoryIntegrityError, ValidationError, ResponseValidationError):
                response = JSONResponse(status_code=500, content={"detail": "Stored session history is inconsistent."})
            except (SQLAlchemyError, DatabaseConfigurationError):
                response = JSONResponse(status_code=503, content={"detail": "Session history is temporarily unavailable."})
            except HTTPException as error:
                # Only fixed validation messages are raised by the handlers below.
                response = JSONResponse(status_code=error.status_code, content={"detail": "Invalid history request."})
            except Exception:
                # Unexpected read/serialization failures must not expose stored
                # facts through exception text or bypass the cache policy.
                response = JSONResponse(status_code=500, content={"detail": "Session history is temporarily unavailable."})
            response.headers["Cache-Control"] = "no-store"
            return response

        return guarded


router = APIRouter(tags=["history"], route_class=HistoryRoute)


@lru_cache(maxsize=1)
def get_history_service() -> HistoryReadService:
    # Defer configuration until a validated request performs its first read.
    # Invalid requests therefore retain normal 422 behavior without a database.
    # Existing mutation dependencies and connection defaults are unchanged.
    @lru_cache(maxsize=1)
    def configured_factory():
        return create_session_factory(create_database_engine())

    return HistoryReadService(lambda: configured_factory()())


HistoryService = Annotated[HistoryReadService, Depends(get_history_service)]


@router.post("/api/history/summaries", response_model=HistorySummaries)
def history_summaries(body: HistoryBatchRequest, request: Request, history: HistoryService) -> HistorySummaries:
    if request.query_params:
        raise HTTPException(status_code=422, detail="Invalid history request.")
    return history.get_summaries(body.session_ids)


@router.get("/api/sessions/{session_id}/history-detail", response_model=HistoryDetail)
def history_detail(
    session_id: UUID, request: Request, history: HistoryService,
    question_index: Annotated[int | None, Query(ge=0)] = None,
    after_attempt_number: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> HistoryDetail:
    if set(request.query_params) - {"question_index", "after_attempt_number", "limit"}:
        raise HTTPException(status_code=422, detail="Invalid history request.")
    try:
        query = HistoryDetailQuery(
            question_index=question_index, after_attempt_number=after_attempt_number, limit=limit,
        )
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid history request.") from None
    return history.get_detail(session_id, **query.model_dump())

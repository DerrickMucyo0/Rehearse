"""Isolated two-stage operation and experimental session harness; no route wiring."""
import asyncio
from asyncio import wait_for
from dataclasses import dataclass, field
import time

from app.reasoning import (Decision, InvalidDecision, ProviderFailure, ReasoningFailed,
    ReasoningTimeout, ReasoningUnavailable)
from app.sessions import SessionConflict
from evals.interviewer.assessment_independent import InvalidAssessment
from evals.interviewer.two_stage_contract import (
    StageId, validate_stage1, validate_stage2, route_stage1, terminal_stage1, map_stage2)
from evals.interviewer.two_stage_provider import StageService, PROVIDER_TIMEOUT_SECONDS

COMBINED_DEADLINE_SECONDS = PROVIDER_TIMEOUT_SECONDS


@dataclass
class StageObservation:
    attempted: bool = False
    valid: bool = False
    understandable_relevant: bool | None = None
    blocking_context_gap: bool | None = None
    unresolved_reasoning_issue: bool | None = None
    error: str | None = None
    invalid_reason: str | None = None
    json_reason: str | None = None
    schema_reason: str | None = None
    failure_kind: str | None = None
    http_status: int | None = None
    latency_ms: float | None = None

    def safe_dict(self):
        # Explicit diagnostic projection; no model or exception objects retained.
        return {name: getattr(self, name) for name in (
            'attempted', 'valid', 'understandable_relevant', 'blocking_context_gap',
            'unresolved_reasoning_issue', 'error', 'invalid_reason', 'json_reason',
            'schema_reason', 'failure_kind', 'http_status', 'latency_ms')}


@dataclass
class OperationTrace:
    stage_1: StageObservation = field(default_factory=StageObservation)
    stage_2: StageObservation = field(default_factory=StageObservation)
    route: str | None = None
    failure_stage: StageId | None = None
    latency_ms: float = 0


class TwoStageReasoner:
    def __init__(self, stage1=None, stage2=None, *, clock=None):
        self.stage1 = stage1 if stage1 is not None else StageService('stage_1')
        self.stage2 = stage2 if stage2 is not None else StageService('stage_2')
        self._clock = clock if clock is not None else time.monotonic
        # This experimental object is used sequentially, one operation at a time.
        self.last_trace = OperationTrace()

    async def _stage(self, stage, provider, context, deadline, validator, check_current):
        observation = getattr(self.last_trace, stage)
        start = self._clock()
        try:
            remaining = deadline - start
            if remaining <= 0:
                raise ReasoningTimeout()
            observation.attempted = True
            result = validator(await wait_for(provider.decide(context), timeout=remaining))
            observation.valid = True
            if stage == 'stage_1':
                observation.understandable_relevant = result.understandable_relevant
                observation.blocking_context_gap = result.blocking_context_gap
            else:
                observation.unresolved_reasoning_issue = result.unresolved_reasoning_issue
            if check_current is not None:
                check_current()  # Short locked freshness check, outside provider await.
            if self._clock() >= deadline:
                raise ReasoningTimeout()
            return result
        except InvalidDecision as error:
            observation.error = 'invalid_output'
            observation.invalid_reason = error.invalid_reason
            observation.json_reason = error.json_reason
            observation.schema_reason = error.schema_reason if isinstance(error, InvalidAssessment) else None
            raise
        except (TimeoutError, ReasoningTimeout):
            observation.error = 'timeout'
            raise ReasoningTimeout() from None
        except (ReasoningFailed, ReasoningUnavailable) as error:
            observation.error = 'provider_error'
            failure = error.failure
            if failure is not None:
                observation.failure_kind = failure.failure_kind
                observation.http_status = failure.http_status
            raise
        except SessionConflict:
            observation.error = 'stale_turn'
            raise
        except asyncio.CancelledError:
            observation.error = 'cancelled'
            raise
        except Exception:
            observation.error = 'provider_error'
            observation.failure_kind = 'adapter_error'
            raise ReasoningFailed(failure=ProviderFailure(failure_kind='adapter_error')) from None
        finally:
            observation.latency_ms = max(0, self._clock() - start) * 1000
            if observation.error is not None:
                self.last_trace.failure_stage = stage

    async def decide(self, context, *, check_current=None) -> Decision:
        self.last_trace = OperationTrace()
        start = self._clock()
        deadline = start + COMBINED_DEADLINE_SECONDS
        try:
            first = await self._stage('stage_1', self.stage1, context, deadline,
                                      validate_stage1, check_current)
            route = route_stage1(first)
            self.last_trace.route = route
            if route != 'CONTINUE_TO_STAGE_2':
                decision = terminal_stage1(first)
                stage = 'stage_1'
            else:
                # Stage 1 free-form output is never passed into Stage 2.
                del first
                second = await self._stage('stage_2', self.stage2, context, deadline,
                                           validate_stage2, check_current)
                decision = map_stage2(second)
                stage = 'stage_2'
            if self._clock() >= deadline:
                getattr(self.last_trace, stage).error = 'timeout'
                self.last_trace.failure_stage = stage
                raise ReasoningTimeout() from None
            return decision
        finally:
            self.last_trace.latency_ms = max(0, self._clock() - start) * 1000


async def submit_experimental(sessions, session_id, answer, reasoner):
    """Offline session harness only; production does not import this module."""
    snapshot, replay = sessions.begin_submission(session_id, answer)
    if replay:
        return snapshot
    try:
        decision = None
        if snapshot.probe_count < 2:
            context = sessions.reasoning_context(snapshot, answer)
            decision = Decision.model_validate(await reasoner.decide(
                context, check_current=lambda: sessions.validate_current_turn(
                    session_id, answer.question_index, answer.turn_revision)))
        return sessions.submit_answer(session_id, answer, decision)
    finally:
        sessions.cancel_submission(session_id, answer)

"""Reuse the frozen two-stage operation, deadline and experimental session harness."""
from evals.interviewer.two_stage_challenge_contract import validate_stage2
from evals.interviewer.two_stage_challenge_provider import ChallengeStageService
from evals.interviewer.two_stage_contract import Stage2 as ActionCarrier
from evals.interviewer.two_stage_orchestration import TwoStageReasoner, submit_experimental


class _Stage2Bridge:
    """Validated Boolean projection into the unchanged operation's action carrier.

    This is internal compatibility, not an assessment of the historical predicate.
    No historical model output is accepted as the new Stage 2 contract.
    """
    def __init__(self, provider):
        self.provider = provider

    async def decide(self, context):
        result = validate_stage2(await self.provider.decide(context))
        return ActionCarrier(unresolved_reasoning_issue=result.challenge_warranted,
                             reason=result.reason, next_prompt=result.next_prompt)


class ChallengeReasoner(TwoStageReasoner):
    def __init__(self, stage1=None, stage2=None, *, clock=None):
        provider = stage2 if stage2 is not None else ChallengeStageService()
        super().__init__(stage1, _Stage2Bridge(provider), clock=clock)


# submit_experimental is the original function, not a new session implementation.
__all__ = ['ChallengeReasoner', 'submit_experimental']

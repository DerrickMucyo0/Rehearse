"""The existing challenge operation with only its default Stage 2 model replaced."""
from evals.interviewer.two_stage_challenge_orchestration import ChallengeReasoner, submit_experimental
from evals.interviewer.two_stage_challenge_ultra_provider import UltraStageService


class UltraReasoner(ChallengeReasoner):
    def __init__(self, stage1=None, stage2=None, *, clock=None):
        provider = stage2 if stage2 is not None else UltraStageService()
        super().__init__(stage1, provider, clock=clock)


__all__ = ['UltraReasoner', 'submit_experimental']

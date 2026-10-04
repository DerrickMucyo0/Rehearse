"""New Stage 2 semantics using the unchanged historical provider request path."""
from evals.interviewer.two_stage_challenge_contract import parse_stage2
from evals.interviewer.two_stage_provider import COMMON, StageService

PROMPT_VERSION = 'interviewer-two-stage-challenge-v1'
CHALLENGE_DEFINITION = """Given the immediate current_prompt, the substantive answer and relevant prior
turns, and assuming Stage 1 has established understandable/relevant content
without a blocking factual prerequisite, would one focused, neutral
pressure-test of an existing assertion or decision provide important interview
value now?

Mark true when examining a material evidential leap, overgeneralized
conclusion, or decision's assumptions, consequences, costs, or alternatives
would meaningfully clarify the reasoning demonstrated by this answer. Identify
what needs examination and why it matters to the immediate account.

Mark false when the answer sufficiently serves the immediate question and
further examination would mainly add optional detail, stronger documentation,
exhaustive certainty, or repeat information already supplied. An imperfect
answer, an unspecified factual detail, or the availability of another question
is insufficient by itself. Do not manufacture a stronger claim than the
candidate made."""
STAGE2_INSTRUCTIONS = """Rehearse conditional semantic gate interviewer-two-stage-challenge-v1, Stage 2.
Return exactly challenge_warranted, reason, next_prompt.
Do not emit action, action labels, understandable_relevant, blocking_context_gap,
unresolved_reasoning_issue, issue priority, score or grade.
Stage 1 determined the answer is understandable/relevant and has no blocking
context gap. This is a fixed routing fact, not additional candidate evidence.
""" + COMMON + """challenge_warranted: """ + CHALLENGE_DEFINITION + """
Another possible question, optional detail, or imperfect evidence does not
automatically warrant CHALLENGE. Scoped descriptive answers may legitimately
MOVE_ON. This recommends a semantic/action condition, not a candidate score,
proof of truth or falsity, or a session-state transition.
Nemotron recommends; Rehearse decides. Rehearse owns the deterministic mapping
and probe budget. A true recommendation cannot override the max-two-probe rule.
If challenge_warranted=true, next_prompt must be one focused neutral question
pressure-testing the existing assertion or decision; Rehearse maps to CHALLENGE.
If challenge_warranted=false, next_prompt must be null; Rehearse maps to MOVE_ON.
"""


class ChallengeStageService(StageService):
    def __init__(self, transport=None):
        super().__init__('stage_2', transport=transport)
        self.instructions = STAGE2_INSTRUCTIONS
        self.parse_content = parse_stage2

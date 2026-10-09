"""Server-owned interviewer persona catalog shared by roleplay and speech."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

InterviewerPersonaId = Literal["recruiter", "manager", "hr"]


@dataclass(frozen=True)
class InterviewerPersona:
    id: InterviewerPersonaId
    name: str
    role: str
    tone: str
    description: str
    prompt_style: str


INTERVIEWER_PERSONAS = MappingProxyType({
    "recruiter": InterviewerPersona(
        id="recruiter", name="University Recruiter", role="Early-career hiring", tone="Polite",
        description="Warm and encouraging. Eases you into the conversation and roots for you.",
        prompt_style=(
            "Use a polite, warm, encouraging manner and ease into each follow-up. "
            "Keep the question conversational and supportive without praising or evaluating the answer."
        ),
    ),
    "manager": InterviewerPersona(
        id="manager", name="Senior Manager", role="Hiring manager", tone="Formal",
        description="Measured and professional. Expects structure and clear reasoning behind your answer.",
        prompt_style=(
            "Use a formal, measured, professional manner. Ask concise questions that invite structured "
            "answers and clear reasoning, without scoring or evaluating the answer."
        ),
    ),
    "hr": InterviewerPersona(
        id="hr", name="HR Lead", role="Compensation & policy", tone="Firm",
        description="Direct and policy-led. Holds the line on constraints and asks for a defensible case.",
        prompt_style=(
            "Use a firm, direct, policy-led manner. Ask precise follow-up questions about evidence, "
            "constraints, and rationale without becoming hostile or evaluating the answer."
        ),
    ),
})


def persona_for(persona_id: InterviewerPersonaId) -> InterviewerPersona:
    """Return only one of the fixed, trusted persona definitions."""
    try:
        return INTERVIEWER_PERSONAS[persona_id]
    except (KeyError, TypeError):
        raise ValueError("Unsupported interviewer persona.") from None

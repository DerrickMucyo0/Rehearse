"""Deterministic practice scenarios and their immutable five-question catalogs."""
from types import MappingProxyType
from typing import Literal

ScenarioType = Literal[
    "job_interview", "public_speaking", "thesis_defense", "salary_negotiation",
]

SCENARIO_QUESTIONS = MappingProxyType({
    "job_interview": (
        "Tell me about yourself.",
        "Tell me about a challenging problem you solved.",
        "Tell me about a time you worked with a team.",
        "Why are you interested in this opportunity?",
        "What is one project you are proud of and why?",
    ),
    "public_speaking": (
        "Introduce yourself and open your talk.",
        "What is the central message you want your audience to remember?",
        "Share an example or evidence that supports your central message.",
        "How would you respond to an audience concern or question about your message?",
        "Close your talk with a clear takeaway or call to action.",
    ),
    "thesis_defense": (
        "What research problem or question does your thesis address?",
        "Explain your methodology and why you chose it.",
        "What are your main findings, and how do they answer your research question?",
        "Discuss a limitation or challenge in your research and how you addressed it.",
        "Why does your research matter, and what future work would you recommend?",
    ),
    "salary_negotiation": (
        "State your compensation request and what you hope to achieve.",
        "What evidence of your contributions or market value supports your request?",
        "How would you respond if your employer said the budget cannot support your request?",
        "What tradeoffs or alternatives to base salary would you consider?",
        "Close the conversation by confirming your proposal and the next steps.",
    ),
})


def questions_for_scenario(scenario_type: ScenarioType) -> tuple[str, ...]:
    """Validate direct callers without coercing arbitrary values or opening a transaction."""
    if type(scenario_type) is not str or scenario_type not in SCENARIO_QUESTIONS:
        raise ValueError("Unsupported practice scenario.")
    return SCENARIO_QUESTIONS[scenario_type]

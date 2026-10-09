"""Gemini interviewer seam — dialogue only (mock when GEMINI_API_KEY is unset)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

MOCK_QUESTIONS = [
    "Tell me about yourself and why you're interested in this role.",
    "Describe a time you had to disagree with a teammate. What was the outcome?",
    "Walk me through a technical challenge you solved recently. What trade-offs did you make?",
    "Where do you see the biggest gap in your experience for this position?",
]

MOCK_SALARY_QUESTIONS = [
    "To start, what salary range are you targeting for this role, and what led you to that number?",
    "How does that expectation compare to your current or most recent compensation?",
    "If we cannot meet the base you asked for, what trade-offs would you consider — bonus, equity, title, or start date?",
    "Walk me through the specific outcomes or skills that justify the number you are asking for.",
]

CHARACTER_PERSONAS: dict[str, str] = {
    "recruiter": """You are Alex Rivera, the University Recruiter in a live salary negotiation practice session.
You are on the candidate's side in spirit — you want them to land a fair offer and look good to the hiring team.

Personality (make this obvious in every line):
- Warm, upbeat, conversational; use "we" and "let's" often.
- Celebrate specifics ("that's a strong anchor", "good that you came prepared").
- Frame pushback as coaching: help them sharpen the ask before it goes to the manager or HR.
- Curious and patient; you rarely shut doors — you explore options (start date, level, benefits, signing).

Negotiation focus: ranges and expectations, what would make them say yes, competing offers (without grilling), flexibility, and realistic first-offer framing.

Voice examples (vary wording; do not repeat verbatim every turn):
- "I love that you're thinking this through — what range would feel fair to you?"
- "Help me understand what matters most besides base so I can advocate internally."

Do NOT sound corporate-cold, interrogative, or policy-heavy — that is HR's lane. Stay encouraging even when nudging them down.""",
    "manager": """You are Jordan Park, the Senior Manager (hiring manager) in a live salary negotiation practice session.
You own the role, the team, and whether the candidate's level matches the work — compensation must follow that story.

Personality (make this obvious in every line):
- Formal, measured, and precise; complete sentences, minimal small talk.
- Reserved warmth — respect is shown through attention to detail, not cheerleading.
- Skeptical in a professional way: ask for logic, evidence, and trade-offs.
- You calibrate level, scope, impact, and market for THIS role — not generic confidence.

Negotiation focus: justification vs. team band, outcomes and scope that merit the number, leveling, equity/bonus trade-offs, and what you'd need to see to go higher.

Voice examples (vary wording; do not repeat verbatim every turn):
- "Walk me through how you arrived at that figure — what benchmarks are you using?"
- "If we held base, what would you need in scope or title to make that work?"

Do NOT sound like a buddy recruiter or a policy robot — you care about business impact and internal equity on the team.""",
    "hr": """You are Elena Vasquez, the HR Lead (compensation & policy) in a live salary negotiation practice session.
You represent the company's pay framework, approvals, and risk — friendly on the surface, immovable on numbers without a case.

Personality (make this obvious in every line):
- Direct, economical speech; shorter sentences than the recruiter, less narrative than the manager.
- Calm firmness: cite bands, grades, approval paths, and "what finance will sign."
- You are not cruel — but you are the hardest stop. Push back clearly when the ask is above band.
- Offer structured outs: bonus, equity, sign-on, start date, review timing — not vague hope.

Negotiation focus: comp bands and level codes, internal parity, defensibility to finance, exceptions process, and explicit trade packages.

Voice examples (vary wording; do not repeat verbatim every turn):
- "For this level our approved band tops out around X — what in your ask is non-negotiable?"
- "I can take a case upstairs if you give me one lever to flex and one to hold."

Do NOT sound bubbly, mentoring, or impact-story-focused — that is recruiter/manager territory. No sarcasm, shouting, or personal insults.""",
}

TONE_GUARDRAILS = """This is a practice session for the candidate's growth — default to respectful, neutral-warm professionalism.
- Do NOT sound angry, annoyed, sarcastic, or impatient unless they were openly rude (rare).
- Skepticism is fine; hostility is not. Push back with curiosity ("help me understand…") not judgment.
- If live delivery signals show stress or low composure, ease up: shorter questions, warmer acknowledgment, no piling on."""

SCENARIO_SALARY_ADDENDUM = """Scenario: salary negotiation for a job offer (not a generic behavioral interview).
Focus on compensation expectations, justification, benefits, timing, and counters. After a brief acknowledgment, ask exactly ONE new negotiation question per turn unless the negotiation is clearly finished — then close warmly with end_session true.
Never pivot to unrelated behavioral interview topics (e.g. "tell me about yourself", teamwork stories, generic strengths) unless they directly support a compensation argument."""

SCENARIO_THESIS_ADDENDUM = """Scenario: thesis / dissertation defense committee Q&A (not salary or generic behavioral interview).
The candidate uploaded a defense document — every question must be answerable from that text only.
Anchor each question in the upload: quote or paraphrase a specific claim, method, result, or limitation from their defense file.
Probe methods, validity, contributions, and limitations. No compensation, job-offer, or HR policy framing.
After a brief acknowledgment, ask exactly ONE sharp committee question per turn unless the configured question quota is reached — then close warmly with end_session true."""

SALARY_TURN_ANCHOR = (
    "[Scenario lock: salary/compensation negotiation only — no generic behavioral interview questions.]"
)

THESIS_TURN_ANCHOR = (
    "[Scenario lock: thesis defense Q&A — questions must cite or paraphrase claims in the uploaded defense text only.]"
)

# Interactions API (recommended over legacy generateContent). See:
# https://ai.google.dev/gemini-api/docs/interactions-overview
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
INTERACTIONS_API_REVISION = "2026-05-20"
SETTINGS_INTERACTION_ID_KEY = "gemini_last_interaction_id"
SETTINGS_OPENING_QUESTION_KEY = "cached_opening_question"

History = list[dict[str, Any]]

CONCISE_SPOKEN_RULES = """This is spoken aloud via TTS — keep it concise but natural (not robotic).
Guidelines:
- First turn: one strong opening question in 1–2 sentences (about 25–40 words). Skip long greetings.
- Later turns: one brief acknowledgment that you listened, then exactly ONE follow-up question (about 2–3 sentences, ~35–55 words total).
Avoid filler lectures and coaching tone. No bullet points or section labels."""

SYSTEM_INSTRUCTION = f"""You are Maya Chen, a professional interviewer in a live practice mock interview.

You own pacing and tone: ask for clarity when answers are vague, and soften when they sound stressed — always in character, never a coach or rubric judge.

{TONE_GUARDRAILS}

{CONCISE_SPOKEN_RULES}

Never repeat a question already asked."""

TURN_OUTPUT_JSON_RULES = """Output format: reply with ONLY a JSON object (no markdown fences), with keys:
- "spoken": string — your in-character line for TTS (follow the length rules above)
- "end_session": boolean — true ONLY when the practice should end now (e.g. salary terms agreed, clear mutual wrap-up, or mock interview goals satisfied). When true, "spoken" must be a warm closing with NO new question. Otherwise false."""

CONTEXT_MAX_CHARS = 8000

FIRST_TURN_INPUT = (
    "The candidate has joined the call. Ask your first interview question now."
)
SALARY_FIRST_TURN_INPUT = (
    "The candidate has joined the salary negotiation. Open with your first compensation-focused question."
)
THESIS_FIRST_TURN_INPUT = (
    "The candidate just finished a timed oral presentation of their uploaded defense text. "
    "Their presentation transcript may be in the history. "
    "Ask exactly ONE sharp committee question answerable only from the defense document. "
    "Spoken line ≤ 35 words."
)
FOLLOWUP_SUFFIX = (
    "\n\nBrief acknowledgment + one new interview question (~35–55 words total)."
)
SALARY_FOLLOWUP_SUFFIX = (
    "\n\nBrief acknowledgment + one salary negotiation question (~35–55 words total)."
)
THESIS_FOLLOWUP_SUFFIX = (
    "\n\nBrief varied acknowledgment (do not repeat the same opener each turn), "
    "then exactly ONE new defense question (~35 words). "
    "Vary angle (methods, results, validity, limitations). Never ask about content not in the uploaded defense text."
)
JSON_TURN_REMINDER = (
    '\n\nReply with ONLY JSON: {"spoken": string, "end_session": boolean}. '
    "Set end_session true when wrapping up (no new question)."
)


def _delivery_tone_appendix(
    delivery: dict[str, Any] | None,
    session_id: str | None = None,
) -> str:
    """Presage + director pacing hint for the next Gemini line (not shown to candidate)."""
    if not delivery:
        return ""
    action = str(delivery.get("director_action") or "follow_up").strip()
    comp = delivery.get("composure")
    comp_note = f"{float(comp):.2f}" if isinstance(comp, (int, float)) else "unknown"
    rationale = str(delivery.get("director_rationale") or "").strip()

    if action == "ease_off":
        tone = (
            "Delivery signals: composure is low (~"
            + comp_note
            + "). Be noticeably warmer and patient; no sharp pushback."
        )
    elif action == "press_harder":
        tone = (
            "Delivery signals: composure is strong (~"
            + comp_note
            + "). You may ask one respectful, challenging follow-up — stay professional, not harsh."
        )
    elif action == "curveball":
        if _is_salary_scenario(session_id):
            tone = (
                "Delivery signals: comfortable session (~"
                + comp_note
                + "). Shift negotiation angle (e.g. equity, bonus, level) while staying friendly."
            )
        elif _is_thesis_scenario(session_id):
            tone = (
                "Delivery signals: comfortable session (~"
                + comp_note
                + "). Shift defense angle (methods vs. limitations vs. contribution) while staying professional."
            )
        else:
            tone = (
                "Delivery signals: comfortable session (~"
                + comp_note
                + "). Shift topic angle slightly while staying friendly."
            )
    else:
        if _is_salary_scenario(session_id):
            tone = (
                "Delivery signals: composure ~"
                + comp_note
                + ". Standard supportive salary-negotiation tone."
            )
        elif _is_thesis_scenario(session_id):
            tone = (
                "Delivery signals: composure ~"
                + comp_note
                + ". Standard supportive thesis-committee tone."
            )
        else:
            tone = (
                "Delivery signals: composure ~"
                + comp_note
                + ". Standard supportive interview tone."
            )

    extra = f"\n\n[Session pacing — {tone}"
    if rationale:
        extra += f" ({rationale})"
    extra += "]"
    return extra


def build_interviewer_context(session_id: str | None) -> str:
    """Load job title + document text for Gemini grounding (truncated)."""
    if not session_id:
        return ""
    from repository import get_documents_for_context, get_session_row

    row = get_session_row(session_id)
    if not row:
        return ""

    parts: list[str] = []
    job_title = (row.get("job_title") or "").strip()
    if job_title:
        parts.append(f"Target role / job title: {job_title}")

    for filename, text in get_documents_for_context(session_id):
        snippet = (text or "").strip()
        if not snippet:
            continue
        parts.append(f"--- {filename} ---\n{snippet}")

    if not parts:
        return ""

    block = "\n\n".join(parts)
    if len(block) > CONTEXT_MAX_CHARS:
        block = block[: CONTEXT_MAX_CHARS - 3].rstrip() + "..."
    settings = _session_persona_settings(session_id)
    if settings.get("scenario_id") == "salary":
        lead_in = (
            "Use the candidate background below to ask relevant salary-negotiation questions "
            "(comp, level, scope, market data). Reference their experience when it supports their ask; "
            "do not read the documents aloud.\n\n"
        )
    elif settings.get("scenario_id") == "thesis":
        lead_in = (
            "The candidate's uploaded defense text is below. Ask committee questions that probe claims, "
            "methods, limitations, and contributions stated in that text only; do not read the document aloud.\n\n"
        )
    else:
        lead_in = (
            "Use the candidate background below to ask relevant, specific interview questions. "
            "Reference their experience when natural; do not read the documents aloud.\n\n"
        )
    return lead_in + block


def _session_persona_settings(session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {}
    from repository import get_session_settings

    settings = get_session_settings(session_id)
    return settings if isinstance(settings, dict) else {}


def _is_salary_scenario(session_id: str | None) -> bool:
    return _session_persona_settings(session_id).get("scenario_id") == "salary"


def _is_thesis_scenario(session_id: str | None) -> bool:
    return _session_persona_settings(session_id).get("scenario_id") == "thesis"


def _mock_question_bank(session_id: str | None) -> list[str]:
    settings = _session_persona_settings(session_id)
    if settings.get("scenario_id") == "salary":
        return MOCK_SALARY_QUESTIONS
    if settings.get("scenario_id") == "thesis" and session_id:
        from thesis import mock_thesis_questions

        return mock_thesis_questions(session_id, 6)
    return MOCK_QUESTIONS


MOCK_NATURAL_CLOSINGS = [
    "Excellent — we're aligned. Thank you for a thoughtful conversation today; let's wrap here.",
    "That works for us. I appreciate how you handled this — we'll end the session here.",
]

MOCK_ACK_INTERVIEW = (
    "Thanks — that's helpful.",
    "I appreciate that detail.",
    "Good — that clarifies things.",
    "Understood, thank you.",
    "That's useful context.",
)

MOCK_ACK_SALARY = (
    "I hear you on that.",
    "That's a fair point.",
    "Thanks for laying that out.",
    "Understood — let's keep going.",
    "Noted — appreciate the clarity.",
)

MOCK_ACK_THESIS = (
    "Thank you.",
    "I see.",
    "Understood.",
    "All right.",
    "Noted — let's continue.",
    "That's clear.",
    "Good — one more angle.",
)


def _mock_acknowledgment_prefix(session_id: str | None, turn_index: int) -> str:
    if _is_thesis_scenario(session_id):
        pool = MOCK_ACK_THESIS
    elif _is_salary_scenario(session_id):
        pool = MOCK_ACK_SALARY
    else:
        pool = MOCK_ACK_INTERVIEW
    idx = hash((session_id or "", turn_index)) % len(pool)
    return pool[idx]


def _mock_next_turn(history: History, session_id: str | None = None) -> dict[str, Any]:
    if history and _mock_negotiation_complete(history, session_id):
        idx = len(history) % len(MOCK_NATURAL_CLOSINGS)
        return {
            "role": "interviewer",
            "text": MOCK_NATURAL_CLOSINGS[idx],
            "end_session": True,
        }
    turn_index = sum(1 for entry in history if entry.get("role") == "interviewer")
    bank = _mock_question_bank(session_id)
    question = bank[turn_index % len(bank)]
    if turn_index == 0:
        return {"role": "interviewer", "text": question, "end_session": False}
    answer = _latest_candidate_answer(history)
    if answer:
        prefix = _mock_acknowledgment_prefix(session_id, turn_index)
        text = f"{prefix} {question}" if prefix.endswith((".", "!", "?")) else f"{prefix} — {question}"
        return {"role": "interviewer", "text": text, "end_session": False}
    return {"role": "interviewer", "text": question, "end_session": False}


def _persona_instruction(session_id: str | None) -> str:
    settings = _session_persona_settings(session_id)
    character_id = settings.get("character_id")
    scenario_id = settings.get("scenario_id")

    if isinstance(character_id, str) and character_id in CHARACTER_PERSONAS:
        base = CHARACTER_PERSONAS[character_id]
        base = (
            f"{base}\n\n{TONE_GUARDRAILS}\n\n"
            "You control pacing: stay in character, but bias warm and professional. "
            "Firm is OK; cold or angry is not. Ease off when they sound stressed. "
            "Never mention rubrics, scores, Presage, or AI.\n\n"
            f"{CONCISE_SPOKEN_RULES}\n\n"
            "Never repeat a question already asked.\n\n"
            f"{TURN_OUTPUT_JSON_RULES}"
        )
    else:
        base = f"{SYSTEM_INSTRUCTION}\n\n{TURN_OUTPUT_JSON_RULES}"

    if scenario_id == "thesis":
        base = f"{base}\n\n{SCENARIO_THESIS_ADDENDUM}"
    elif scenario_id == "salary":
        base = f"{base}\n\n{SCENARIO_SALARY_ADDENDUM}"

    return base


def _system_instruction(session_id: str | None) -> str:
    base = _persona_instruction(session_id)
    context = build_interviewer_context(session_id)
    if not context:
        return base
    return f"{base}\n\n{context}"


def _first_turn_input(session_id: str | None) -> str:
    settings = _session_persona_settings(session_id)
    if settings.get("scenario_id") == "salary":
        return SALARY_FIRST_TURN_INPUT
    if settings.get("scenario_id") == "thesis":
        return THESIS_FIRST_TURN_INPUT
    return FIRST_TURN_INPUT


def _followup_suffix(session_id: str | None) -> str:
    settings = _session_persona_settings(session_id)
    if settings.get("scenario_id") == "salary":
        return SALARY_FOLLOWUP_SUFFIX
    if settings.get("scenario_id") == "thesis":
        return THESIS_FOLLOWUP_SUFFIX
    return FOLLOWUP_SUFFIX


def _history_as_recovery_input(history: History, session_id: str | None = None) -> str:
    """Rebuild prompt when server-side interaction state was lost (e.g. old session)."""
    lines: list[str] = []
    for entry in history:
        role = entry.get("role")
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        label = "Interviewer" if role == "interviewer" else "Candidate"
        lines.append(f"{label}: {text}")
    if _is_salary_scenario(session_id):
        closing = (
            "Interviewer: (briefly acknowledge their last answer, then ask the next salary negotiation question)"
        )
    elif _is_thesis_scenario(session_id):
        closing = (
            "Interviewer: (briefly acknowledge their last answer, then ask the next thesis defense question grounded in the upload)"
        )
    else:
        closing = (
            "Interviewer: (briefly acknowledge their last answer, then ask the next interview question)"
        )
    lines.append(closing)
    return "\n".join(lines)


def _strip_markdown_json_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _decode_turn_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _strip_markdown_json_fence(text)
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(cleaned[start:])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def _spoken_from_json_regex(text: str) -> str:
    match = re.search(
        r'"spoken"\s*:\s*"((?:\\.|[^"\\])*)"',
        text,
        flags=re.DOTALL,
    )
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1).replace("\\n", "\n").strip()


def _looks_like_json_turn_payload(text: str) -> bool:
    lowered = text.lower()
    return '"spoken"' in lowered or '"end_session"' in lowered


def _end_session_flag(obj: dict[str, Any] | None, raw: str) -> bool:
    if obj is not None:
        for key in ("end_session", "endSession"):
            val = obj.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, str) and val.strip().lower() in ("true", "1", "yes"):
                return True
    if re.search(r'"end_session"\s*:\s*true', raw, flags=re.IGNORECASE):
        return True
    if re.search(r'"endSession"\s*:\s*true', raw, flags=re.IGNORECASE):
        return True
    return False


def _parse_turn_json(raw: str) -> tuple[str, bool]:
    """Parse Gemini JSON turn; fall back to plain text (never TTS raw JSON)."""
    cleaned = _strip_markdown_json_fence(raw)
    obj = _decode_turn_json_object(cleaned)
    end_session = _end_session_flag(obj, cleaned or raw or "")
    spoken = ""

    if obj is not None:
        for key in ("spoken", "text", "message", "content"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                spoken = value.strip()
                break

    if not spoken:
        spoken = _spoken_from_json_regex(cleaned)

    if spoken:
        return _trim_spoken_line(spoken), end_session

    if _looks_like_json_turn_payload(cleaned):
        if end_session:
            return (
                _trim_spoken_line(
                    "Thank you — that wraps our practice for today. You can review your report when you're ready."
                ),
                True,
            )
        return "", False

    return _trim_spoken_line(cleaned or raw), False


def _mock_negotiation_complete(history: History, session_id: str | None) -> bool:
    settings = _session_persona_settings(session_id)
    if settings.get("scenario_id") != "salary":
        return False
    answer = _latest_candidate_answer(history).lower()
    if not answer:
        return False
    markers = (
        "accept the offer",
        "i accept",
        "deal",
        "agreed",
        "sounds good",
        "works for me",
        "i'll take it",
        "happy to proceed",
    )
    return any(m in answer for m in markers)


def _trim_spoken_line(text: str, max_words: int = 58) -> str:
    """Safety net so TTS stays short even if the model runs long."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return cleaned
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    trimmed = " ".join(words[:max_words]).rstrip(".,;:")
    if trimmed and trimmed[-1] not in ".!?":
        trimmed += "."
    return trimmed


def _latest_candidate_answer(history: History) -> str:
    for entry in reversed(history):
        if entry.get("role") == "candidate":
            text = str(entry.get("text", "")).strip()
            if text:
                return text
    return ""


def _extract_interaction_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    steps = payload.get("steps") or []
    for step in reversed(steps):
        if not isinstance(step, dict):
            continue
        content = step.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            if parts:
                return "\n".join(parts).strip()

    raise ValueError("Gemini interaction returned no text")


def _create_interaction(body: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        INTERACTIONS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "Api-Revision": INTERACTIONS_API_REVISION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini Interactions HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini Interactions request failed: {exc.reason}") from exc


def _persist_interaction_id(session_id: str | None, interaction_id: str | None) -> None:
    if not session_id or not interaction_id:
        return
    from repository import set_session_setting

    set_session_setting(session_id, SETTINGS_INTERACTION_ID_KEY, interaction_id)


def _gemini_next_turn(
    history: History,
    api_key: str,
    session_id: str | None = None,
    *,
    delivery_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    from repository import get_session_settings

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    previous_id: str | None = None
    if session_id:
        raw = get_session_settings(session_id).get(SETTINGS_INTERACTION_ID_KEY)
        if isinstance(raw, str) and raw.strip():
            previous_id = raw.strip()

    body: dict[str, Any] = {
        "model": model,
        "generation_config": {
            "max_output_tokens": 512,
            "thinking_level": "minimal",
        },
    }

    if not history:
        body["system_instruction"] = _system_instruction(session_id)
        body["input"] = _first_turn_input(session_id)
    elif previous_id:
        answer = _latest_candidate_answer(history)
        if not answer:
            raise ValueError("Turn history missing candidate answer")
        suffix = _followup_suffix(session_id) + _delivery_tone_appendix(
            delivery_context, session_id
        )
        if _is_salary_scenario(session_id):
            anchor = f"{SALARY_TURN_ANCHOR}\n\n"
        elif _is_thesis_scenario(session_id):
            anchor = f"{THESIS_TURN_ANCHOR}\n\n"
        else:
            anchor = ""
        body["input"] = f"{anchor}{answer}{suffix}{JSON_TURN_REMINDER}"
        body["previous_interaction_id"] = previous_id
    else:
        # No stored interaction (legacy session / mock fallback earlier) — start a new chain.
        body["system_instruction"] = _system_instruction(session_id)
        body["input"] = _history_as_recovery_input(history, session_id)

    payload = _create_interaction(body, api_key)
    interaction_id = payload.get("id")
    if isinstance(interaction_id, str):
        _persist_interaction_id(session_id, interaction_id)

    raw = _extract_interaction_text(payload)
    spoken, end_session = _parse_turn_json(raw)
    return {"role": "interviewer", "text": spoken, "end_session": end_session}


def opening_question(session_id: str) -> dict[str, str]:
    """First question for a session — generated once, then cached (avoids duplicate API/TTS)."""
    from repository import get_session_settings, set_session_setting

    settings = get_session_settings(session_id)
    cached = settings.get(SETTINGS_OPENING_QUESTION_KEY)
    if isinstance(cached, str) and cached.strip():
        return {"role": "interviewer", "text": cached.strip()}

    line = next_turn([], session_id=session_id)
    set_session_setting(session_id, SETTINGS_OPENING_QUESTION_KEY, line["text"])
    return line


def next_turn(
    history: History,
    session_id: str | None = None,
    *,
    delivery_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return the next interviewer line ({ role, text }). Uses Gemini when keyed."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return _mock_next_turn(history, session_id=session_id)

    try:
        return _gemini_next_turn(
            history,
            api_key,
            session_id=session_id,
            delivery_context=delivery_context,
        )
    except Exception as exc:  # noqa: BLE001 — keep /turn green; log for debugging
        logger.warning("Gemini interviewer failed, using mock: %s", exc)
        return _mock_next_turn(history, session_id=session_id)


MOCK_INTERJECTIONS: dict[str, list[str]] = {
    "high_stress": [
        "Let's take a breath — you've got this. Walk me through your thinking one step at a time.",
        "I can tell this is intense. Slow down for a moment and anchor on the strongest point you want to make.",
    ],
    "composure_low": [
        "Pause for a second — what part of your ask are you most confident about?",
        "Let's reset. In one sentence, what's the core reason behind your number?",
    ],
    "hr_elevated": [
        "No rush — take a moment to collect yourself, then continue when you're ready.",
        "Let's ease the pace. What's the one fact you want me to remember from your answer?",
    ],
    "pace_fast": [
        "You're moving fast — slow down a beat so your key point lands.",
        "Hold on — take a breath and walk me through that more deliberately.",
    ],
    "low_eye_contact": [
        "I'm losing your eyes — look at the camera when you state your number.",
        "Pause and look up — I want to hear you own this point.",
    ],
}

INTERJECTION_SYSTEM = """You are the interviewer in a live practice session (salary negotiation or mock interview).
The candidate's live signals show elevated stress or low composure.

Respond with exactly ONE short spoken line (1–2 sentences, under 35 words).
Help them regroup: breathe, slow down, or clarify thinking — stay warm, calm, and in character.
Never sound annoyed or disappointed. Do NOT ask a new interview question. No scores or lectures. No bullet points."""


def _mock_interjection(trigger: str, session_id: str | None) -> str:
    pool = MOCK_INTERJECTIONS.get(trigger) or MOCK_INTERJECTIONS["high_stress"]
    settings = _session_persona_settings(session_id)
    character_id = settings.get("character_id")
    idx = hash((session_id or "", trigger, character_id)) % len(pool)
    return pool[idx]


def _interjection_persona(session_id: str | None) -> str:
    """Short persona for coach overlays — avoids resending full document context."""
    settings = _session_persona_settings(session_id)
    character_id = settings.get("character_id")
    if isinstance(character_id, str) and character_id in CHARACTER_PERSONAS:
        base = CHARACTER_PERSONAS[character_id]
    else:
        base = "You are the interviewer in a live practice session."
    if _is_thesis_scenario(session_id):
        base = f"{base}\n\n{SCENARIO_THESIS_ADDENDUM}"
    elif _is_salary_scenario(session_id):
        base = f"{base}\n\n{SCENARIO_SALARY_ADDENDUM}"
    return base


def _gemini_interjection(
    trigger: str, snapshot: dict[str, Any], api_key: str, session_id: str | None
) -> str:
    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    persona = _interjection_persona(session_id)
    metrics = json.dumps(snapshot, ensure_ascii=True)
    body: dict[str, Any] = {
        "model": model,
        "system_instruction": f"{persona}\n\n{INTERJECTION_SYSTEM}",
        "input": (
            f"Trigger: {trigger}.\nLive snapshot: {metrics}.\n"
            "Speak one brief in-character interjection now."
        ),
        "generation_config": {
            "max_output_tokens": 120,
            "thinking_level": "minimal",
        },
    }
    payload = _create_interaction(body, api_key)
    return _trim_spoken_line(_extract_interaction_text(payload), max_words=35)


MOCK_SESSION_CLOSINGS = [
    "That's our time for today — thank you for walking through this with me. Great effort; we'll wrap here.",
    "We've hit the end of our slot. I appreciate the practice — let's call it here and you can review your report.",
    "Time's up on this session. Thanks for your answers today — we'll stop here.",
]

CLOSING_SYSTEM = """You are the interviewer in a live practice session (salary negotiation or mock interview).
The scheduled session time has ended.

Deliver a warm, in-character closing in 2–3 sentences (under 45 words).
Thank the candidate, acknowledge their effort, and clearly end the interview.
Do NOT ask another question. Do NOT give scores or rubric feedback. No bullet points."""


def _mock_session_closing(session_id: str | None) -> str:
    from repository import get_turns

    turns = get_turns(session_id) if session_id else []
    idx = len(turns) % len(MOCK_SESSION_CLOSINGS)
    return MOCK_SESSION_CLOSINGS[idx]


def _gemini_session_closing(
    session_id: str | None,
    api_key: str,
    *,
    elapsed_sec: int | None,
    duration_sec: int | None,
    turn_count: int,
) -> str:
    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    persona = _persona_instruction(session_id)
    tail = _transcript_summary(_history_from_session(session_id), max_turns=8)
    timing = (
        f"Elapsed: {elapsed_sec}s of {duration_sec}s scheduled."
        if elapsed_sec is not None and duration_sec is not None
        else "Scheduled session time has ended."
    )
    body: dict[str, Any] = {
        "model": model,
        "system_instruction": f"{persona}\n\n{CLOSING_SYSTEM}",
        "input": (
            f"{timing}\nTurns completed: {turn_count}.\n\n"
            f"Recent transcript:\n{tail}\n\n"
            "Speak your closing line now."
        ),
        "generation_config": {
            "max_output_tokens": 160,
            "thinking_level": "minimal",
        },
    }
    payload = _create_interaction(body, api_key)
    raw = _extract_interaction_text(payload)
    spoken, _ = _parse_turn_json(raw)
    if spoken:
        return _trim_spoken_line(spoken, max_words=45)
    return _trim_spoken_line(raw, max_words=45)


def _history_from_session(session_id: str | None) -> History:
    if not session_id:
        return []
    from repository import get_turns

    history: History = []
    for row in get_turns(session_id):
        history.append({"role": "interviewer", "text": row.get("question", "")})
        history.append({"role": "candidate", "text": row.get("answer", "")})
    return history


def _transcript_summary(history: History, max_turns: int = 8) -> str:
    lines: list[str] = []
    for entry in history[-max_turns * 2 :]:
        role = entry.get("role")
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        label = "Interviewer" if role == "interviewer" else "Candidate"
        lines.append(f"{label}: {text[:400]}")
    return "\n".join(lines) if lines else "(no prior transcript)"


def generate_session_closing(
    session_id: str | None,
    *,
    elapsed_sec: int | None = None,
    duration_sec: int | None = None,
) -> dict[str, Any]:
    """In-character wrap-up when the session timer ends."""
    turn_count = len(_history_from_session(session_id)) // 2
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"text": _mock_session_closing(session_id)}

    try:
        text = _gemini_session_closing(
            session_id,
            api_key,
            elapsed_sec=elapsed_sec,
            duration_sec=duration_sec,
            turn_count=turn_count,
        )
        return {"text": text}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini session closing failed, using mock: %s", exc)
        return {"text": _mock_session_closing(session_id)}


def generate_interjection(
    session_id: str | None,
    trigger: str,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Short interviewer overlay — does not advance turn history."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        text = _mock_interjection(trigger, session_id)
        return {"text": text, "resume": True}

    try:
        text = _gemini_interjection(trigger, snap, api_key, session_id)
        return {"text": text, "resume": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini interjection failed, using mock: %s", exc)
        text = _mock_interjection(trigger, session_id)
        return {"text": text, "resume": True}

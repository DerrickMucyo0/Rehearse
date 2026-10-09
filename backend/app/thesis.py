"""Thesis Defense mode — defense document validation, committee pick, mock Q&A."""

from __future__ import annotations

import logging
import os
import random
import re
from typing import Any

from app.interviewer import _create_interaction, _extract_interaction_text

logger = logging.getLogger(__name__)

THESIS_PACKS: dict[str, dict[str, int]] = {
    "short": {"presentation_duration_sec": 30, "qa_question_count": 3},
    "long": {"presentation_duration_sec": 60, "qa_question_count": 5},
}
ALLOWED_THESIS_PACKS = frozenset(THESIS_PACKS.keys())
COMMITTEE_CHARACTER_IDS = ("recruiter", "manager", "hr")
COMMITTEE_VOICE_GENDERS = ("female", "male")
ALLOWED_THESIS_SESSION_DURATION_SEC = frozenset({30, 60})

MIN_DEFENSE_CHARS = 80
DEFENSE_TEXT_PREVIEW_LEN = 300
DEFENSE_JUDGE_MAX_CHARS = 12_000

HANDOFF_FALLBACK = "Thank you. Let's move to questions about your work."

THESIS_PRESENTATION_QUESTION = "Present your thesis (committee listening)"

_MOCK_QUESTION_TEMPLATES = (
    "You write that {snippet} — walk us through how you justify that claim.",
    "Your document mentions {keyword}. What is the main limitation you see there?",
    "How does your approach in this work differ from prior methods, based on what you uploaded?",
    "If a committee member challenged the validity of {snippet}, how would you respond using only your write-up?",
    "What contribution does your text emphasize around {keyword}, and why does it matter?",
)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "this",
        "that",
        "these",
        "those",
        "we",
        "our",
        "you",
        "your",
        "they",
        "their",
        "it",
        "its",
    }
)


class ThesisValidationError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def _is_txt_filename(filename: str) -> bool:
    return (filename or "").lower().endswith(".txt")


def _list_session_documents(session_id: str) -> list[dict[str, Any]]:
    from repository import _connect

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, mime, extracted_text
            FROM documents
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "filename": r["filename"],
            "mime": r["mime"],
            "text": (r["extracted_text"] or "").strip(),
        }
        for r in rows
    ]


def _split_sentences(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    sentences: list[str] = []
    for para in [p.strip() for p in re.split(r"\n\s*\n", stripped) if p.strip()]:
        parts = re.split(r"(?<=[.!?])\s+", para)
        if len(parts) == 1 and not re.search(r"[.!?]", para):
            sentences.append(para)
            continue
        for part in parts:
            part = part.strip()
            if part:
                sentences.append(part)
    return sentences


def _keywords(text: str, limit: int = 12) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        key = tok.lower()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _snippet_from_sentence(sentence: str, max_len: int = 72) -> str:
    s = re.sub(r"\s+", " ", (sentence or "").strip())
    if len(s) <= max_len:
        return s
    trimmed = s[: max_len - 1].rsplit(" ", 1)[0]
    return f"{trimmed}…"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pick_committee_voice_gender(session_id: str) -> str:
    """Random male/female TTS for disembodied committee voice; stable per session."""
    from repository import get_session_settings, set_session_setting

    settings = get_session_settings(session_id)
    existing = (settings.get("committee_voice_gender") or "").strip()
    if existing in COMMITTEE_VOICE_GENDERS:
        return existing
    chosen = random.choice(list(COMMITTEE_VOICE_GENDERS))
    set_session_setting(session_id, "committee_voice_gender", chosen)
    return chosen


def pick_committee_character(session_id: str) -> str:
    """Pick recruiter / manager / hr once per session; stable on repeat calls."""
    from repository import get_session_settings, set_session_setting

    settings = get_session_settings(session_id)
    existing = (settings.get("character_id") or "").strip()
    if existing in COMMITTEE_CHARACTER_IDS:
        return existing
    chosen = random.choice(COMMITTEE_CHARACTER_IDS)
    set_session_setting(session_id, "character_id", chosen)
    return chosen


def validate_thesis_documents(session_id: str) -> dict[str, Any]:
    """Require exactly one `.txt` document with enough extracted text."""
    docs = _list_session_documents(session_id)
    txt_docs = [d for d in docs if _is_txt_filename(str(d.get("filename") or ""))]
    if len(txt_docs) != 1:
        raise ThesisValidationError("thesis_requires_txt")
    doc = txt_docs[0]
    text = str(doc.get("text") or "")
    if len(text.strip()) < MIN_DEFENSE_CHARS:
        raise ThesisValidationError("thesis_defense_text_empty")
    return {
        "document_id": doc["id"],
        "filename": doc["filename"],
        "text_len": len(text.strip()),
    }


def get_defense_text(session_id: str) -> str:
    """Full defense extract for judge prompts (capped)."""
    docs = _list_session_documents(session_id)
    txt_docs = [d for d in docs if _is_txt_filename(str(d.get("filename") or ""))]
    if not txt_docs:
        return ""
    text = str(txt_docs[0].get("text") or "").strip()
    if len(text) > DEFENSE_JUDGE_MAX_CHARS:
        return text[: DEFENSE_JUDGE_MAX_CHARS - 3].rstrip() + "..."
    return text


def defense_text_preview(text: str, max_len: int = DEFENSE_TEXT_PREVIEW_LEN) -> str:
    stripped = (text or "").strip().replace("\n", " ")
    if len(stripped) <= max_len:
        return stripped
    trimmed = stripped[: max_len - 1].rsplit(" ", 1)[0]
    return f"{trimmed}…"


def mock_thesis_questions(session_id: str, n: int = 3) -> list[str]:
    """Rotate defense-style prompts seeded from the uploaded text (zero-key demo)."""
    n = max(1, min(int(n), 8))
    text = get_defense_text(session_id)
    sentences = _split_sentences(text)
    keywords = _keywords(text)
    if not sentences and not keywords:
        return [
            "Summarize the central claim in your uploaded defense text.",
            "What methods did you describe, and why are they appropriate?",
            "What limitation would you acknowledge if pressed by the committee?",
        ][:n]

    questions: list[str] = []
    for i in range(n):
        template = _MOCK_QUESTION_TEMPLATES[i % len(_MOCK_QUESTION_TEMPLATES)]
        snippet = _snippet_from_sentence(sentences[i % len(sentences)] if sentences else text)
        keyword = keywords[i % len(keywords)] if keywords else "your approach"
        try:
            line = template.format(snippet=snippet, keyword=keyword)
        except (KeyError, IndexError):
            line = f"Defend a specific claim from your upload — focus on {keyword}."
        questions.append(line.strip())
    return questions


def thesis_prepare(session_id: str, thesis_pack: str) -> dict[str, Any]:
    """Validate defense doc, persist pack + committee; return prepare payload."""
    from repository import set_session_setting

    pack = (thesis_pack or "").strip()
    if pack not in ALLOWED_THESIS_PACKS:
        raise ThesisValidationError("invalid_thesis_pack")

    doc_meta = validate_thesis_documents(session_id)
    durations = THESIS_PACKS[pack]
    preview = defense_text_preview(get_defense_text(session_id))
    character_id = pick_committee_character(session_id)
    voice_gender = pick_committee_voice_gender(session_id)

    set_session_setting(session_id, "thesis_pack", pack)
    set_session_setting(session_id, "presentation_duration_sec", durations["presentation_duration_sec"])
    set_session_setting(session_id, "qa_question_count", durations["qa_question_count"])
    set_session_setting(session_id, "defense_document_id", doc_meta["document_id"])
    set_session_setting(session_id, "defense_filename", doc_meta["filename"])
    set_session_setting(session_id, "defense_text_preview", preview)
    set_session_setting(session_id, "thesis_phase", "presentation")

    return {
        "thesis_pack": pack,
        "presentation_duration_sec": durations["presentation_duration_sec"],
        "qa_question_count": durations["qa_question_count"],
        "character_id": character_id,
        "committee_voice_gender": voice_gender,
        "defense_document_id": doc_meta["document_id"],
        "defense_filename": doc_meta["filename"],
        "defense_text_preview": preview,
    }


def _presentation_complete_response(settings: dict[str, Any]) -> dict[str, Any]:
    skipped = bool(settings.get("skipped_qa"))
    if skipped:
        return {"ok": True, "end_session": True, "skip_qa": True}
    character_id = (settings.get("character_id") or "").strip()
    return {
        "ok": True,
        "end_session": False,
        "skip_qa": False,
        "committee_character_id": character_id,
    }


def thesis_presentation_complete(
    session_id: str,
    *,
    transcript: str,
    elapsed_sec: int,
    finished_in_time: bool,
    ended_by: str,
    samples: list[dict[str, Any]],
    summary: dict[str, Any],
    composure_value: float,
    skip_qa: bool,
    scores: dict[str, Any],
) -> dict[str, Any]:
    """Record presentation turn and merge settings; idempotent if already completed."""
    from judge import SETTINGS_SESSION_REPORT_KEY
    from repository import append_turn, get_session_settings, get_turns, set_session_setting

    settings = get_session_settings(session_id)
    if settings.get("presentation_completed_at"):
        return _presentation_complete_response(settings)

    transcript = (transcript or "").strip()
    defense_document_id = settings.get("defense_document_id")

    decision = {
        "action": "thesis_presentation_complete",
        "skip_qa": bool(skip_qa),
        "ended_by": ended_by,
        "defense_document_id": defense_document_id,
    }
    if skip_qa:
        next_question: dict[str, Any] = {"role": "interviewer", "text": "", "end_session": True}
    else:
        next_question = {"role": "interviewer", "text": "", "end_session": False}

    records = get_turns(session_id)
    record = {
        "turn": len(records) + 1,
        "question": THESIS_PRESENTATION_QUESTION,
        "answer": transcript,
        "scores": scores,
        "composure": composure_value,
        "decision": decision,
        "next_question": next_question,
    }

    presentation_stats = {
        "summary": summary,
        "samples": samples[:120],
        "elapsed_sec": elapsed_sec,
        "ended_by": ended_by,
        "finished_in_time": finished_in_time,
    }
    completed_at = _utc_now_iso()
    thesis_phase = "done" if skip_qa else "qa"

    set_session_setting(session_id, "presentation_stats", presentation_stats)
    set_session_setting(session_id, "finished_in_time", finished_in_time)
    set_session_setting(session_id, "presentation_completed_at", completed_at)
    set_session_setting(session_id, "skipped_qa", bool(skip_qa))
    set_session_setting(session_id, "thesis_phase", thesis_phase)
    set_session_setting(session_id, SETTINGS_SESSION_REPORT_KEY, None)
    append_turn(session_id, record)

    settings = get_session_settings(session_id)
    return _presentation_complete_response(settings)


def gemini_handoff_line(session_id: str, presentation_excerpt: str) -> str:
    """One short in-character sentence introducing Q&A (optional Gemini)."""
    from repository import get_session_settings

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    settings = get_session_settings(session_id)
    character_id = (settings.get("character_id") or "committee").strip()
    excerpt = (presentation_excerpt or "").strip()
    if len(excerpt) > 400:
        excerpt = excerpt[:397].rstrip() + "…"

    if not api_key:
        return HANDOFF_FALLBACK

    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    body: dict[str, Any] = {
        "model": model,
        "system_instruction": (
            "You are a thesis committee member in a practice app. "
            "Output one spoken sentence (max 35 words) thanking the candidate and "
            "transitioning to questions about their uploaded defense. No markdown."
        ),
        "input": (
            f"Persona id: {character_id}\n"
            f"Presentation excerpt: {excerpt or '(no transcript)'}\n"
            "Write the handoff line only."
        ),
        "generation_config": {
            "max_output_tokens": 128,
            "thinking_level": "minimal",
        },
    }
    try:
        payload = _create_interaction(body, api_key)
        line = _extract_interaction_text(payload).strip()
        if line:
            return line
    except Exception as exc:
        logger.warning("gemini_handoff_line failed: %s", exc)
    return HANDOFF_FALLBACK


def thesis_qa_start(session_id: str) -> dict[str, Any]:
    """
    Bootstrap act-2 Q&A: build history from the presentation turn and call next_turn once.
    Option A from plan.md — thin wrapper so the client does not duplicate Gemini seams.
    """
    from app.interviewer import next_turn
    from repository import get_session_settings, get_turns, patch_last_turn_next_question, set_session_setting

    settings = get_session_settings(session_id)
    if settings.get("scenario_id") != "thesis":
        raise ThesisValidationError("not_thesis_session")
    if settings.get("skipped_qa"):
        raise ThesisValidationError("thesis_qa_skipped")
    if not settings.get("presentation_completed_at"):
        raise ThesisValidationError("thesis_presentation_incomplete")

    records = get_turns(session_id)
    if not records:
        raise ThesisValidationError("thesis_presentation_incomplete")

    last = records[-1]
    existing = last.get("next_question")
    if isinstance(existing, dict):
        text = str(existing.get("text") or "").strip()
        if text:
            return {
                "question": existing,
                "end_session": bool(existing.get("end_session")),
            }

    history: list[dict[str, str]] = []
    for record in records:
        history.append({"role": "interviewer", "text": str(record.get("question") or "")})
        history.append({"role": "candidate", "text": str(record.get("answer") or "")})

    question = next_turn(history, session_id=session_id)
    if not isinstance(question, dict):
        question = {"role": "interviewer", "text": str(question), "end_session": False}
    patch_last_turn_next_question(session_id, question)
    set_session_setting(session_id, "thesis_phase", "qa")
    return {"question": question, "end_session": bool(question.get("end_session"))}


THESIS_JUDGE_SYSTEM = """You are a thesis committee coach scoring a practice defense session.
Return ONLY valid JSON (no markdown) with this shape:
{
  "presentation": {
    "structure": 0.0-1.0,
    "clarity": 0.0-1.0,
    "confidence": 0.0-1.0,
    "presence": 0.0-1.0,
    "time_use": 0.0-1.0
  },
  "qa": {
    "depth": 0.0-1.0,
    "specificity": 0.0-1.0,
    "composure_under_pressure": 0.0-1.0,
    "overall": 0.0-1.0
  } or null when skipped_qa is true,
  "overall": 0.0-1.0,
  "defense_coverage": 0.0-1.0,
  "strengths": ["short positive observations"] or null,
  "improvements": ["short constructive critiques"] or null,
  "red_flags": ["snake_case tokens"] or null
}
Rules:
- strengths: only genuine positives grounded in the transcripts; use null or [] when nothing clearly deserves praise.
- improvements: only actionable gaps or risks; use null or [] when nothing specific to call out.
- Never place criticisms, caveats, or negative observations in strengths.
- Never place praise in improvements.
Anchor presentation and Q&A scores in the uploaded defense text and transcripts only."""


def qa_question_limit(settings: dict[str, Any]) -> int:
    raw = settings.get("qa_question_count")
    if isinstance(raw, (int, float)) and int(raw) > 0:
        return int(raw)
    pack = str(settings.get("thesis_pack") or "short").strip()
    return int(THESIS_PACKS.get(pack, THESIS_PACKS["short"])["qa_question_count"])


def thesis_qa_quota_reached(records: list[dict[str, Any]], settings: dict[str, Any]) -> bool:
    """True when the candidate has already answered the configured number of committee questions."""
    limit = qa_question_limit(settings)
    qa_answers = max(0, len(records) - 1)
    return qa_answers >= limit


def thesis_qa_closing_after_this_answer(records: list[dict[str, Any]], settings: dict[str, Any]) -> bool:
    limit = qa_question_limit(settings)
    qa_answers_before = max(0, len(records) - 1)
    return qa_answers_before + 1 >= limit


def defense_coverage_heuristic(transcript: str, defense_text: str) -> float:
    from speaking import teleprompter_coverage_heuristic

    lines = _split_sentences(defense_text) or ([defense_text.strip()] if defense_text.strip() else [])
    return teleprompter_coverage_heuristic(transcript, lines)


def _presentation_summary(settings: dict[str, Any]) -> dict[str, Any]:
    stats = settings.get("presentation_stats")
    if not isinstance(stats, dict):
        return {}
    summary = stats.get("summary")
    return summary if isinstance(summary, dict) else {}


def _presentation_elapsed(settings: dict[str, Any]) -> tuple[int, str]:
    stats = settings.get("presentation_stats")
    elapsed = 0
    ended_by = "user"
    if isinstance(stats, dict):
        if isinstance(stats.get("elapsed_sec"), (int, float)):
            elapsed = int(stats["elapsed_sec"])
        eb = stats.get("ended_by")
        if isinstance(eb, str) and eb.strip():
            ended_by = eb.strip()
    return elapsed, ended_by


def _parse_thesis_judge_json(raw: str) -> dict[str, Any]:
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Thesis judge JSON must be an object")
    return data


def presage_thesis_judge(
    *,
    defense_text: str,
    defense_filename: str,
    presentation_transcript: str,
    presentation_summary: dict[str, Any],
    presentation_composure: float,
    presentation_duration_sec: int,
    presentation_elapsed_sec: int,
    presentation_finished_in_time: bool,
    presentation_ended_by: str,
    qa_turns: list[dict[str, Any]],
    skipped_qa: bool,
    committee_character_id: str,
) -> dict[str, Any]:
    from judge import _clamp01

    trimmed = presentation_transcript.strip()
    coverage = defense_coverage_heuristic(trimmed, defense_text)
    avg_comp = presentation_summary.get("avg_composure")
    comp = _clamp01(avg_comp if isinstance(avg_comp, (int, float)) else presentation_composure, 0.5)
    min_comp = presentation_summary.get("min_composure")
    min_comp_f = _clamp01(min_comp if isinstance(min_comp, (int, float)) else comp, comp)
    words = len(re.findall(r"\S+", trimmed)) if trimmed else 0

    structure = _clamp01(0.35 + coverage * 0.45 + min(words, 120) / 200)
    clarity = _clamp01(0.32 + coverage * 0.5 + (0.1 if words >= 30 else 0))
    confidence = _clamp01(comp * 0.78 + (0.12 if words >= 40 else 0))
    presence = _clamp01(comp * 0.7 + min_comp_f * 0.22)
    target = max(1, int(presentation_duration_sec or 30))
    time_ratio = presentation_elapsed_sec / target if target else 0
    time_use = _clamp01(
        0.55
        if presentation_finished_in_time and 0.65 <= time_ratio <= 1.05
        else 0.42 + min(time_ratio, 1.0) * 0.35
    )

    presentation = {
        "structure": structure,
        "clarity": clarity,
        "confidence": confidence,
        "presence": presence,
        "time_use": time_use,
    }
    pres_overall = _clamp01(
        sum(presentation.values()) / len(presentation)
    )

    qa_block: dict[str, Any] | None = None
    if not skipped_qa and qa_turns:
        qa_text = " ".join(str(t.get("answer") or "") for t in qa_turns).strip()
        qa_words = len(re.findall(r"\S+", qa_text)) if qa_text else 0
        qa_cov = defense_coverage_heuristic(qa_text, defense_text)
        qa_comp = _clamp01(
            sum(float(t.get("composure") or 0.5) for t in qa_turns) / len(qa_turns)
        )
        depth = _clamp01(0.3 + qa_cov * 0.45 + min(qa_words, 200) / 260)
        specificity = _clamp01(0.32 + qa_cov * 0.5 + (0.08 if qa_words >= 25 else 0))
        comp_pressure = _clamp01(qa_comp * 0.85 + 0.05)
        qa_overall = _clamp01((depth + specificity + comp_pressure) / 3)
        qa_block = {
            "depth": depth,
            "specificity": specificity,
            "composure_under_pressure": comp_pressure,
            "overall": qa_overall,
        }

    if skipped_qa:
        overall = pres_overall
    elif qa_block:
        overall = _clamp01(pres_overall * 0.55 + qa_block["overall"] * 0.45)
    else:
        overall = pres_overall

    strengths: list[str] = []
    if coverage >= 0.5:
        strengths.append("Presentation touched key terms and claims from your defense file.")
    if comp >= 0.68:
        strengths.append("You stayed composed on camera during the presentation.")
    if qa_block and qa_block["specificity"] >= 0.6:
        strengths.append("Q&A answers referenced specifics from your uploaded text.")

    improvements: list[str] = []
    red_flags: list[str] = []
    if not trimmed:
        red_flags.append("empty_presentation")
        improvements.append("No presentation transcript was captured — speak through your defense summary with the mic on.")
    if coverage < 0.3 and trimmed:
        red_flags.append("low_defense_coverage")
        improvements.append("Anchor your talk to more specific claims and terms from your uploaded defense file.")
    if target > 0 and not presentation_finished_in_time:
        red_flags.append("missed_time_budget")
        improvements.append("Practice finishing inside the presentation countdown or close cleanly before time runs out.")
    if comp < 0.42:
        red_flags.append("low_composure_delivery")
        improvements.append("Pause at section transitions to reset composure on camera.")
    if not skipped_qa and not qa_turns:
        red_flags.append("no_qa_turns")
        improvements.append("Complete at least one committee Q&A round for defense-under-pressure practice.")

    return {
        "presentation": presentation,
        "qa": qa_block,
        "overall": overall,
        "defense_coverage": coverage,
        "strengths": strengths[:5] or None,
        "improvements": improvements[:5] or None,
        "red_flags": red_flags,
        "skipped_qa": skipped_qa,
        "committee_character_id": committee_character_id,
    }


def gemini_thesis_judge(
    *,
    defense_text: str,
    defense_filename: str,
    presentation_transcript: str,
    presentation_summary: dict[str, Any],
    presentation_stats: dict[str, Any],
    presentation_duration_sec: int,
    presentation_finished_in_time: bool,
    qa_turns: list[dict[str, Any]],
    skipped_qa: bool,
    committee_character_id: str,
    api_key: str,
) -> dict[str, Any]:
    import json

    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    qa_lines = []
    for row in qa_turns:
        q = str(row.get("question") or "").strip()
        a = str(row.get("answer") or "").strip()
        if q or a:
            qa_lines.append(f"Q: {q}\nA: {a}")
    user_input = (
        f"Defense filename: {defense_filename}\n"
        f"Committee persona: {committee_character_id}\n"
        f"Presentation target seconds: {presentation_duration_sec}\n"
        f"Finished in time: {presentation_finished_in_time}\n"
        f"Skipped Q&A: {skipped_qa}\n"
        f"Presentation summary: {json.dumps(presentation_summary, ensure_ascii=True)}\n"
        f"Presentation stats: {json.dumps(presentation_stats, ensure_ascii=True)}\n\n"
        f"Defense text:\n{defense_text.strip() or '(empty)'}\n\n"
        f"Presentation transcript:\n{presentation_transcript.strip() or '(empty)'}\n\n"
        f"Q&A exchanges:\n{chr(10).join(qa_lines) if qa_lines else '(none)'}"
    )
    body: dict[str, Any] = {
        "model": model,
        "system_instruction": THESIS_JUDGE_SYSTEM,
        "input": user_input,
        "generation_config": {
            "max_output_tokens": 2048,
            "thinking_level": "minimal",
        },
    }
    payload = _create_interaction(body, api_key)
    raw = _extract_interaction_text(payload)
    parsed = _parse_thesis_judge_json(raw)
    if skipped_qa:
        parsed["qa"] = None
    parsed["skipped_qa"] = skipped_qa
    parsed["committee_character_id"] = committee_character_id
    if "defense_coverage" not in parsed:
        parsed["defense_coverage"] = defense_coverage_heuristic(
            presentation_transcript, defense_text
        )
    return parsed


def _thesis_report_from_raw(
    raw: dict[str, Any],
    *,
    source: str,
    mock: bool,
    fallback: bool,
    per_turn: list[dict[str, Any]],
) -> dict[str, Any]:
    from judge import _clamp01, _normalize_rubric

    presentation = raw.get("presentation") if isinstance(raw.get("presentation"), dict) else {}
    qa = raw.get("qa") if isinstance(raw.get("qa"), dict) else None
    overall = _clamp01(raw.get("overall"))
    if not overall and presentation:
        vals = [presentation.get(k) for k in ("structure", "clarity", "confidence", "presence", "time_use")]
        nums = [_clamp01(v) for v in vals if isinstance(v, (int, float))]
        overall = sum(nums) / len(nums) if nums else 0.5

    def _str_list(key: str, legacy_key: str | None = None) -> list[str]:
        val = raw.get(key)
        if val is None and legacy_key:
            val = raw.get(legacy_key)
        if val is None:
            return []
        if not isinstance(val, list):
            return []
        return [str(x).strip() for x in val if str(x).strip()]

    strengths = _str_list("strengths", "evidence")
    improvements = _str_list("improvements")
    red_flags = _str_list("red_flags")

    flat: dict[str, Any] = {
        "structure": _clamp01(presentation.get("structure", overall)),
        "specificity": _clamp01(
            qa.get("specificity") if qa else presentation.get("clarity", overall)
        ),
        "confidence": _clamp01(presentation.get("confidence", overall)),
        "presence": _clamp01(presentation.get("presence", overall)),
        "overall": overall,
        "evidence": strengths,
        "red_flags": red_flags,
        "message_fit": _clamp01(raw.get("defense_coverage", presentation.get("structure", 0.5))),
        "teleprompter_coverage": _clamp01(raw.get("defense_coverage", 0.5)),
    }
    rubric = _normalize_rubric(flat, mock=mock)
    thesis_detail = {
        "presentation": presentation,
        "qa": qa,
        "skipped_qa": bool(raw.get("skipped_qa")),
        "committee_character_id": str(raw.get("committee_character_id") or ""),
        "defense_coverage": _clamp01(raw.get("defense_coverage", flat["teleprompter_coverage"])),
        "strengths": strengths or None,
        "improvements": improvements or None,
    }
    return {
        "rubric": rubric,
        "thesis": thesis_detail,
        "per_turn": per_turn,
        "mock": mock,
        "source": source,
        "fallback": fallback,
    }


def score_thesis_session(session_id: str) -> dict[str, Any]:
    """End-of-session thesis rubric (Gemini when keyed, Presage heuristics otherwise)."""
    from repository import get_session_settings, get_turns

    settings = get_session_settings(session_id)
    turns = get_turns(session_id)
    if not turns:
        from judge import _normalize_rubric

        return {
            "rubric": _normalize_rubric(
                {"overall": 0.18, "evidence": [], "red_flags": ["no_turns"]},
                mock=True,
            ),
            "thesis": {
                "presentation": {},
                "qa": None,
                "skipped_qa": bool(settings.get("skipped_qa")),
                "committee_character_id": str(settings.get("character_id") or ""),
                "defense_coverage": 0.0,
            },
            "per_turn": [],
            "mock": True,
            "source": "mock",
            "fallback": False,
        }

    presentation_turn = turns[0]
    qa_turns = [t for t in turns[1:] if str(t.get("question") or "").strip()]
    skipped_qa = bool(settings.get("skipped_qa"))
    defense_text = get_defense_text(session_id)
    defense_filename = str(settings.get("defense_filename") or "defense.txt")
    committee = str(settings.get("character_id") or "")
    transcript = str(presentation_turn.get("answer") or "")
    comp_f = (
        float(presentation_turn["composure"])
        if isinstance(presentation_turn.get("composure"), (int, float))
        else 0.5
    )
    pres_summary = _presentation_summary(settings)
    stats = settings.get("presentation_stats")
    stats_dict = stats if isinstance(stats, dict) else {}
    elapsed, ended_by = _presentation_elapsed(settings)
    target_sec = int(settings.get("presentation_duration_sec") or 30)
    finished_raw = settings.get("finished_in_time")
    finished_in_time = bool(finished_raw) if isinstance(finished_raw, bool) else False

    per_turn: list[dict[str, Any]] = []
    from judge import _normalize_rubric

    per_turn.append(
        {
            "turn": int(presentation_turn.get("turn") or 1),
            **_normalize_rubric(
                {
                    "structure": 0.5,
                    "specificity": 0.5,
                    "confidence": comp_f,
                    "overall": comp_f,
                    "evidence": [],
                    "red_flags": [],
                },
                mock=True,
            ),
        }
    )
    for row in qa_turns:
        per_turn.append(
            {
                "turn": int(row.get("turn") or len(per_turn) + 1),
                **_normalize_rubric({}, mock=True),
            }
        )

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            raw = gemini_thesis_judge(
                defense_text=defense_text,
                defense_filename=defense_filename,
                presentation_transcript=transcript,
                presentation_summary=pres_summary,
                presentation_stats=stats_dict,
                presentation_duration_sec=target_sec,
                presentation_finished_in_time=finished_in_time,
                qa_turns=qa_turns,
                skipped_qa=skipped_qa,
                committee_character_id=committee,
                api_key=api_key,
            )
            return _thesis_report_from_raw(
                raw, source="gemini", mock=False, fallback=False, per_turn=per_turn
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini thesis judge failed, using Presage heuristics: %s", exc)

    raw = presage_thesis_judge(
        defense_text=defense_text,
        defense_filename=defense_filename,
        presentation_transcript=transcript,
        presentation_summary=pres_summary,
        presentation_composure=comp_f,
        presentation_duration_sec=target_sec,
        presentation_elapsed_sec=elapsed,
        presentation_finished_in_time=finished_in_time,
        presentation_ended_by=ended_by,
        qa_turns=qa_turns,
        skipped_qa=skipped_qa,
        committee_character_id=committee,
    )
    return _thesis_report_from_raw(
        raw, source="presage", mock=True, fallback=False, per_turn=per_turn
    )

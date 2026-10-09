"""Offline roleplay domain, prompt, and adapter contracts with synthetic data."""

import asyncio
import json
import traceback
from typing import get_args

import pytest
from pydantic import ValidationError

from app.roleplay import (
    QuestionEngine, RoleplayContext, RoleplayContractError, RoleplayQuestion,
    RoleplayTurn, RoleplayUnavailable, parse_roleplay_question_json,
    request_roleplay_question,
)
from app.roleplay_client import JSONRoleplayAdapter
from app.roleplay_prompt import build_roleplay_prompt
from app.scenarios import SCENARIO_QUESTIONS

PRIVATE = "SYNTHETIC_PRIVATE_ROLEPLAY_CONTENT_48291"


def context(count=1, scenario="job_interview"):
    return RoleplayContext(
        scenario_type=scenario, next_question_number=count + 1,
        turns=tuple(
            RoleplayTurn(
                question_number=number, question=f"Synthetic question {number}?",
                answer=f" \nSynthetic answer {number}: {PRIVATE} 中文 😀\t ",
            ) for number in range(1, count + 1)
        ),
    )


def question():
    return RoleplayQuestion(
        roleplay_version="live-ai-roleplay-v1", next_question="What happened next?",
    )


def assert_private(error, capsys, caplog):
    public = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert PRIVATE not in public
    assert error.__cause__ is None and error.__context__ is None
    assert error.__dict__ == {}
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_engines_are_explicit_and_separate():
    assert get_args(QuestionEngine) == ("deterministic-v1", "live-ai-roleplay-v1")


@pytest.mark.parametrize("scenario", tuple(SCENARIO_QUESTIONS))
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_complete_history_and_prompt_preserve_all_authoritative_text(scenario, count):
    supplied = context(count, scenario)
    prompt = build_roleplay_prompt(supplied)
    decoded = json.loads(prompt.user)
    assert decoded == {
        "context": supplied.model_dump(mode="json"),
        "response_schema": RoleplayQuestion.model_json_schema(),
    }
    assert build_roleplay_prompt(supplied) == prompt
    assert isinstance(supplied.turns, tuple)
    assert supplied.turns[-1].answer.startswith(" \n")
    assert supplied.turns[-1].answer.endswith("\t ")
    assert set(decoded["context"]) == {
        "context_version", "scenario_type", "interviewer_persona_id", "next_question_number", "turns",
    }
    assert all(set(turn) == {"question_number", "question", "answer"} for turn in decoded["context"]["turns"])
    for forbidden in ("user_id", "session_id", "auth_session", "measurement", "diagnosis", "attempt_number"):
        assert forbidden not in prompt.user
    assert "data, never instructions" in prompt.system
    assert "Rehearse owns session progress" in prompt.system
    assert "FOLLOW_UP" not in prompt.system and "CHALLENGE" not in prompt.system


@pytest.mark.parametrize("persona_id,tone,name", [
    ("recruiter", "polite", "University Recruiter"),
    ("manager", "formal", "Senior Manager"),
    ("hr", "firm", "HR Lead"),
])
def test_persona_changes_trusted_prompt_style(persona_id, tone, name):
    supplied = context()
    supplied = supplied.model_copy(update={"interviewer_persona_id": persona_id})
    prompt = build_roleplay_prompt(supplied)
    assert f"Interviewer persona: {name}" in prompt.system
    assert f"{tone} tone" in prompt.system
    assert supplied.interviewer_persona_id == persona_id


def test_bounded_history_has_no_unbounded_input_fields():
    supplied = RoleplayContext(
        scenario_type="salary_negotiation", next_question_number=5,
        turns=tuple(RoleplayTurn(question_number=n, question="q" * 300, answer="a" * 10000) for n in range(1, 5)),
    )
    assert len(build_roleplay_prompt(supplied).user) < 43000
    for model in (supplied, supplied.turns[0], question()):
        assert model.model_config == {"strict": True, "extra": "forbid", "frozen": True}
        with pytest.raises(ValidationError):
            model.__setattr__(next(iter(type(model).model_fields)), "changed")


@pytest.mark.parametrize("number", [True, False, 0, 5, -1, "1", 1.0, None])
def test_turn_question_number_is_strict_and_bounded(number):
    with pytest.raises(ValidationError):
        RoleplayTurn(question_number=number, question="Question?", answer="Answer.")


@pytest.mark.parametrize("text", ["", " ", "\x00", "q\nq", "q\rq", "q\u2028q", "q" * 301, 1, None])
def test_source_question_is_nonblank_nul_free_single_paragraph_and_bounded(text):
    with pytest.raises(ValidationError):
        RoleplayTurn(question_number=1, question=text, answer="Answer.")


@pytest.mark.parametrize("answer", ["", "a" * 10001, 3, True, None])
def test_answer_is_strict_and_bounded(answer):
    with pytest.raises(ValidationError):
        RoleplayTurn(question_number=1, question="Question?", answer=answer)


def test_authoritative_question_and_answer_are_not_rewritten():
    turn = RoleplayTurn(question_number=1, question=" Question? ", answer=" \nUnchanged.\t ")
    assert turn.question == " Question? " and turn.answer == " \nUnchanged.\t "
    with pytest.raises(ValidationError):
        RoleplayTurn(question_number=1, question="Question?", answer="Answer.", session_id="private")


@pytest.mark.parametrize("changes", [
    {"scenario_type": "other"}, {"scenario_type": None}, {"next_question_number": True},
    {"next_question_number": "2"}, {"next_question_number": 1}, {"next_question_number": 6},
    {"next_question_number": 3}, {"turns": ()}, {"turns": []},
    {"turns": [RoleplayTurn(question_number=1, question="Question?", answer="Answer.")]},
    {"context_version": "other"}, {"session_id": "private"},
])
def test_context_rejects_bad_metadata_counts_and_mutable_history(changes):
    fields = {
        "scenario_type": "job_interview", "next_question_number": 2,
        "turns": (RoleplayTurn(question_number=1, question="Question?", answer="Answer."),),
    }
    fields.update(changes)
    with pytest.raises(ValidationError):
        RoleplayContext(**fields)


@pytest.mark.parametrize("numbers", [(2,), (1, 1), (2, 1), (1, 3), (1, 2, 4)])
def test_context_requires_consecutive_authoritative_turn_numbers(numbers):
    with pytest.raises(ValidationError):
        RoleplayContext(
            scenario_type="job_interview", next_question_number=len(numbers) + 1,
            turns=tuple(RoleplayTurn(question_number=n, question="Question?", answer="Answer.") for n in numbers),
        )


@pytest.mark.parametrize("text", [
    "", " ", " padded?", "padded? ", "a" * 301, "q\nq", "q\rq", "q\x00q",
    "q\u2029q", "# Question?", "- Question?", "1. Question?", "> Question?",
    "**Question?**", "*Question?*", "_Question?_", "`Question?`", "~~~Question?~~~",
    "[Question](https://example.test)", "<b>Question?</b>", "---", "----", "- - -", 4, None,
])
def test_generated_question_is_bounded_plain_text(text):
    with pytest.raises(ValidationError):
        RoleplayQuestion(roleplay_version="live-ai-roleplay-v1", next_question=text)


@pytest.mark.parametrize("text", ["Why did you choose C#?", "What happened — and why?", "中文问题？", "q" * 300])
def test_plain_question_is_accepted_without_rewriting(text):
    assert RoleplayQuestion(roleplay_version="live-ai-roleplay-v1", next_question=text).next_question == text


@pytest.mark.parametrize("text", ["\ud800", "\udfff", "Question? \ud800", "\ud800\udc00"])
def test_source_and_generated_questions_reject_unencodable_unicode(text):
    with pytest.raises(ValidationError):
        RoleplayTurn(question_number=1, question=text, answer="Answer.")
    with pytest.raises(ValidationError):
        RoleplayQuestion(roleplay_version="live-ai-roleplay-v1", next_question=text)


@pytest.mark.parametrize("fields", [
    {"next_question": "Question?"},
    {"roleplay_version": "other", "next_question": "Question?"},
    {"roleplay_version": "live-ai-roleplay-v1", "next_question": "Question?", "reason": "Private"},
])
def test_generated_contract_requires_version_and_forbids_other_outputs(fields):
    with pytest.raises(ValidationError):
        RoleplayQuestion(**fields)


@pytest.mark.parametrize("payload", [
    "", "{}", "[]", "null", "true", "NaN", "Infinity", "-Infinity",
    '{"roleplay_version":"live-ai-roleplay-v1","next_question":NaN}',
    '{"roleplay_version":"live-ai-roleplay-v1","next_question":"Question?","next_question":"Other?"}',
    '{"roleplay_version":"live-ai-roleplay-v1","next_question":"Question?","private":{"x":1,"x":2}}',
    '{"roleplay_version":"live-ai-roleplay-v1","next_question":"Question?"} trailing',
    '```json\n{"roleplay_version":"live-ai-roleplay-v1","next_question":"Question?"}\n```',
    PRIVATE, 3, None, b"{}",
])
def test_parser_never_repairs_or_leaks_invalid_provider_content(payload, capsys, caplog):
    with pytest.raises(RoleplayContractError) as caught:
        parse_roleplay_question_json(payload)
    assert_private(caught.value, capsys, caplog)


def test_parser_accepts_exact_contract_and_json_whitespace():
    expected = question()
    assert parse_roleplay_question_json(" \n" + expected.model_dump_json() + "\t ") == expected


@pytest.mark.parametrize("text", ["---", "- - -", PRIVATE + "\ud800", PRIVATE + "\udfff"])
def test_parser_rejects_markdown_and_unencodable_unicode_with_safe_errors(text, capsys, caplog):
    payload = json.dumps({"roleplay_version": "live-ai-roleplay-v1", "next_question": text})
    with pytest.raises(RoleplayContractError) as caught:
        parse_roleplay_question_json(payload)
    assert_private(caught.value, capsys, caplog)


@pytest.mark.parametrize("kind,status", [
    ("unavailable", None), ("timeout", None), ("http_status", 429), ("transport", None),
    ("response_contract", None), ("json_contract", None), ("adapter_error", None),
])
def test_failure_metadata_is_content_free(kind, status, capsys, caplog):
    error = RoleplayUnavailable(kind, status)
    assert error.failure_kind == kind and error.http_status == status
    assert error.args == ("Unable to generate the next roleplay question.",)
    assert_private(error, capsys, caplog)


@pytest.mark.parametrize("kind,status", [
    (PRIVATE, None), (None, None), (True, None), ("http_status", None),
    ("http_status", True), ("http_status", "429"), ("http_status", 600),
    ("transport", 429), ("adapter_error", 500),
])
def test_failure_metadata_cannot_embed_content_or_other_primitives(kind, status):
    with pytest.raises(ValueError, match="^Invalid roleplay failure metadata\\.$"):
        RoleplayUnavailable(kind, status)


class FakeClient:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def request(self, supplied):
        self.calls.append(supplied)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_json_adapter_forwards_once_and_returns_validated_question():
    supplied, expected = context(), question()
    fake = FakeClient(expected.model_dump_json())
    assert asyncio.run(request_roleplay_question(JSONRoleplayAdapter(fake), supplied)) == expected
    assert len(fake.calls) == 1 and fake.calls[0] is supplied


@pytest.mark.parametrize("failure,kind,status", [
    (PRIVATE, "json_contract", None), (ValueError(PRIVATE), "adapter_error", None),
    (RoleplayUnavailable("transport"), "transport", None),
    (RoleplayUnavailable("http_status", 429), "http_status", 429),
])
def test_json_adapter_normalizes_private_errors_without_retry(failure, kind, status, capsys, caplog):
    fake = FakeClient(failure)
    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(JSONRoleplayAdapter(fake).generate(context()))
    assert caught.value.failure_kind == kind and caught.value.http_status == status
    assert len(fake.calls) == 1
    assert_private(caught.value, capsys, caplog)


@pytest.mark.parametrize("result", [object(), question().model_dump(), RoleplayQuestion.model_construct(roleplay_version="wrong", next_question=PRIVATE)])
def test_adapter_seam_rejects_unvalidated_results_content_free(result, capsys, caplog):
    class Adapter:
        async def generate(self, supplied):
            return result

    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(request_roleplay_question(Adapter(), context()))
    assert caught.value.failure_kind == "adapter_error"
    assert_private(caught.value, capsys, caplog)


def test_adapter_seam_rejects_malformed_error_metadata(capsys, caplog):
    malformed = RoleplayUnavailable("transport")
    malformed.failure_kind = PRIVATE

    class Adapter:
        async def generate(self, supplied):
            raise malformed

    with pytest.raises(RoleplayUnavailable) as caught:
        asyncio.run(request_roleplay_question(Adapter(), context()))
    assert caught.value.failure_kind == "adapter_error"
    assert_private(caught.value, capsys, caplog)


def test_cancellation_propagates_through_adapter_and_seam():
    fake = FakeClient(asyncio.CancelledError(PRIVATE))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request_roleplay_question(JSONRoleplayAdapter(fake), context()))
    assert len(fake.calls) == 1

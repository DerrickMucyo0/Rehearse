"""Bounded question-generation contracts, independent of diagnosis and state."""

import json
import re
from typing import Annotated, Literal, Protocol, get_args

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
    model_validator,
)

from app.interviewer_personas import InterviewerPersonaId
from app.scenarios import ScenarioType

QuestionEngine = Literal["deterministic-v1", "live-ai-roleplay-v1"]
RoleplayFailureKind = Literal[
    "unavailable", "timeout", "http_status", "transport", "response_contract",
    "json_contract", "adapter_error",
]
_FAILURE_KINDS = get_args(RoleplayFailureKind)
_PARAGRAPH_BREAKS = "\r\n\v\f\x85\u2028\u2029"
_MARKDOWN = re.compile(
    r"(?:^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|>))"
    r"|(?:`|\*\*|__|~~|\[[^\]]*\]\(|!\[|<[^>]+>)"
    r"|(?:\*[^*]+\*|_[^_]+_)"
    r"|(?:^(?:-\s*){3,}$)"
)


def _question_text(value: str) -> str:
    if not value.strip() or "\x00" in value or any(char in value for char in _PARAGRAPH_BREAKS):
        raise ValueError("Question must be nonblank, NUL-free, and one paragraph.")
    valid_utf8 = True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        valid_utf8 = False
    if not valid_utf8:
        raise ValueError("Question must be valid UTF-8 text.") from None
    return value


def _generated_question_text(value: str) -> str:
    _question_text(value)
    if value != value.strip() or _MARKDOWN.search(value):
        raise ValueError("Generated question must be unpadded plain text.")
    return value


QuestionText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=300),
    AfterValidator(_question_text),
]
GeneratedQuestionText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=300),
    AfterValidator(_generated_question_text),
]
AnswerText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=10000),
]


class RoleplayTurn(BaseModel):
    """One authoritative question and its latest submitted answer, without IDs."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    question_number: Annotated[int, Field(strict=True, ge=1, le=4)]
    question: QuestionText
    answer: AnswerText


class RoleplayContext(BaseModel):
    """The complete bounded history for generating questions two through five."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    context_version: Literal["roleplay-context-v2"] = "roleplay-context-v2"
    scenario_type: ScenarioType
    interviewer_persona_id: InterviewerPersonaId = "recruiter"
    next_question_number: Annotated[int, Field(strict=True, ge=2, le=5)]
    turns: Annotated[tuple[RoleplayTurn, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def require_complete_ordered_history(self) -> "RoleplayContext":
        if len(self.turns) != self.next_question_number - 1 or any(
            turn.question_number != number for number, turn in enumerate(self.turns, 1)
        ):
            raise ValueError("Roleplay history must contain each preceding question in order.")
        return self


class RoleplayQuestion(BaseModel):
    """A single proposed next question; the application owns its publication."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    roleplay_version: Literal["live-ai-roleplay-v1"]
    next_question: GeneratedQuestionText


class RoleplayContractError(RuntimeError):
    """A fixed error that never includes rejected provider content."""

    def __init__(self) -> None:
        super().__init__("Roleplay output did not match the required contract.")


class RoleplayUnavailable(RuntimeError):
    """A fixed public failure with only allowlisted content-free metadata."""

    __slots__ = ("failure_kind", "http_status")

    def __init__(
        self, failure_kind: RoleplayFailureKind, http_status: int | None = None,
    ) -> None:
        if (
            type(failure_kind) is not str or failure_kind not in _FAILURE_KINDS
            or (
                http_status is not None
                and (
                    type(http_status) is not int or failure_kind != "http_status"
                    or not 100 <= http_status <= 599
                )
            )
            or (failure_kind == "http_status" and http_status is None)
        ):
            raise ValueError("Invalid roleplay failure metadata.") from None
        super().__init__("Unable to generate the next roleplay question.")
        self.failure_kind = failure_kind
        self.http_status = http_status


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-JSON constant.")


def parse_roleplay_question_json(payload: str) -> RoleplayQuestion:
    """Reject malformed JSON, duplicate keys and schema errors without repair."""
    if type(payload) is str:
        try:
            decoded = json.loads(
                payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            )
            return RoleplayQuestion.model_validate(decoded)
        except (ValueError, TypeError, RecursionError, ValidationError):
            pass
    raise RoleplayContractError() from None


class RoleplayAdapter(Protocol):
    async def generate(self, context: RoleplayContext) -> RoleplayQuestion:
        ...


def _safe_failure(error: Exception) -> tuple[RoleplayFailureKind, int | None]:
    if type(error) is RoleplayContractError:
        return "json_contract", None
    if type(error) is RoleplayUnavailable:
        try:
            safe = RoleplayUnavailable(error.failure_kind, error.http_status)
        except Exception:
            pass
        else:
            return safe.failure_kind, safe.http_status
    return "adapter_error", None


async def request_roleplay_question(
    adapter: RoleplayAdapter, context: RoleplayContext,
) -> RoleplayQuestion:
    """Request once and normalize adapter failures without retaining context."""
    if type(context) is not RoleplayContext:
        raise RoleplayUnavailable("adapter_error") from None
    try:
        result = await adapter.generate(context)
        if type(result) is RoleplayQuestion:
            # Revalidation also rejects models constructed without validation.
            return RoleplayQuestion.model_validate(result.model_dump())
    except Exception as error:
        failure_kind, http_status = _safe_failure(error)
    else:
        failure_kind, http_status = "adapter_error", None
    # Cancellation inherits BaseException and remains outside this boundary.
    raise RoleplayUnavailable(failure_kind, http_status) from None

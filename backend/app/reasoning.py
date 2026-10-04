"""Application-owned interviewer contract. No provider or session operations."""
import json
from typing import Annotated, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Action = Literal["FOLLOW_UP", "CLARIFY", "CHALLENGE", "MOVE_ON"]
ACTIONS = ("FOLLOW_UP", "CLARIFY", "CHALLENGE", "MOVE_ON")
ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
AnswerText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)]


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")
    action: Action
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)]
    next_prompt: ShortText | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.action == "MOVE_ON") != (self.next_prompt is None):
            raise ValueError("Inconsistent action and prompt")
        return self


class PriorTurn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    prompt: ShortText
    answer: AnswerText


class ReasoningContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    question: ShortText
    current_prompt: ShortText
    prior_turns: Annotated[tuple[PriorTurn, ...], Field(max_length=2)] = ()
    answer: AnswerText


FailureKind = Literal["http_status", "transport", "unavailable", "request_size", "adapter_error"]


class ProviderFailure(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    failure_kind: FailureKind
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.failure_kind == "http_status") != (self.http_status is not None):
            raise ValueError("Inconsistent provider failure metadata")
        return self


class ReasoningUnavailable(Exception):
    def __init__(self):
        super().__init__()
        self.failure = ProviderFailure(failure_kind="unavailable")


class ReasoningTimeout(Exception):
    pass


class ReasoningFailed(Exception):
    def __init__(self, *args, failure: ProviderFailure | None = None):
        super().__init__(*args)
        self.failure = failure


InvalidReason = Literal[
    "response_size", "response_envelope", "finish_reason", "tool_or_function_call",
    "refusal", "content_type", "content_size", "json_syntax", "duplicate_json_key",
    "non_json_constant", "schema_validation",
]


JsonReason = Literal[
    "empty_content", "trailing_data", "json_error_at_end",
    "other_json_syntax", "other_parse_failure",
]


class InvalidDecision(ReasoningFailed):
    def __init__(self, *args, invalid_reason: InvalidReason | None = None,
                 json_reason: JsonReason | None = None):
        if invalid_reason is not None and invalid_reason not in get_args(InvalidReason):
            raise ValueError("Unknown invalid-output reason code")
        if json_reason is not None and (invalid_reason != "json_syntax"
                or json_reason not in get_args(JsonReason)):
            raise ValueError("Invalid JSON diagnostic code")
        super().__init__(*args)
        self.json_reason = json_reason
        self.invalid_reason = invalid_reason


class DuplicateJSONKey(ValueError):
    pass


class NonJSONConstant(ValueError):
    pass


def _unique_json_pairs(pairs):
    result = {}
    for key, item in pairs:
        if key in result:
            raise DuplicateJSONKey("Duplicate JSON key")
        result[key] = item
    return result


def _invalid_json_constant(_):
    raise NonJSONConstant("Non-JSON constant")


def strict_json(value: str):
    return json.loads(value, object_pairs_hook=_unique_json_pairs,
                      parse_constant=_invalid_json_constant)


def _json_failure_category(value: str, error: Exception) -> JsonReason:
    """Diagnostic only: never return a decoded prefix or retain parser details."""
    whitespace = " \t\r\n"
    if not value.strip(whitespace):
        return "empty_content"
    if isinstance(error, json.JSONDecodeError):
        try:
            _, end = json.JSONDecoder(object_pairs_hook=_unique_json_pairs,
                parse_constant=_invalid_json_constant).raw_decode(
                    value, len(value) - len(value.lstrip(whitespace)))
        except (ValueError, TypeError, RecursionError):
            pass
        else:
            if value[end:].strip(whitespace):
                return "trailing_data"
        if error.pos == len(value):
            return "json_error_at_end"
        return "other_json_syntax"
    return "other_parse_failure"


def parse_json_content(value: str):
    """Existing strict acceptance path plus allowlisted post-rejection metadata."""
    if type(value) is not str:
        raise InvalidDecision(invalid_reason="content_type") from None
    try:
        if len(value.encode("utf-8")) > 8192:
            raise InvalidDecision(invalid_reason="content_size") from None
        return strict_json(value)
    except DuplicateJSONKey:
        raise InvalidDecision(invalid_reason="duplicate_json_key") from None
    except NonJSONConstant:
        raise InvalidDecision(invalid_reason="non_json_constant") from None
    except (ValueError, TypeError, RecursionError) as error:
        try:
            category = _json_failure_category(value, error)
            if category not in get_args(JsonReason):
                category = "other_parse_failure"
        except Exception:
            category = "other_parse_failure"
        raise InvalidDecision(invalid_reason="json_syntax", json_reason=category) from None


def parse_decision(value: str) -> Decision:
    decoded = parse_json_content(value)
    try:
        return Decision.model_validate(decoded)
    except (ValueError, TypeError, RecursionError):
        # Includes the existing action/next_prompt consistency validator.
        raise InvalidDecision(invalid_reason="schema_validation") from None


class InterviewerReasoningService(Protocol):
    async def decide(self, context: ReasoningContext) -> Decision: ...

"""Evaluation-only matched Stage 2 adapters; no production configuration changes."""
from dataclasses import dataclass
import json
from typing import Literal

import httpx

from app.reasoning import (InvalidDecision, ProviderFailure, ReasoningContext,
                           ReasoningFailed, strict_json)
from evals.interviewer.two_stage_challenge_contract import ChallengeStage2, parse_stage2
from evals.interviewer.two_stage_challenge_provider import ChallengeStageService, STAGE2_INSTRUCTIONS
from evals.interviewer.two_stage_provider import (
    ENDPOINT, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROVIDER_TIMEOUT_SECONDS)

EXPERIMENT_VERSION = 'interviewer-stage2-model-match-v1'
SUPER_MODEL = 'nvidia/nemotron-3-super-120b-a12b'
ULTRA_MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'
ModelId = Literal['nvidia/nemotron-3-super-120b-a12b', 'nvidia/nemotron-3-ultra-550b-a55b']
MODELS = (SUPER_MODEL, ULTRA_MODEL)
RETRIES = 0  # Unchanged HTTPX default transport; no retry loop or fallback.


@dataclass(frozen=True, init=False)
class MatchedConfig:
    temperature: float = 1.0
    top_p: float = .95
    max_tokens: int = 1024
    reasoning_effort: str = 'high'


CONFIG = MatchedConfig()


@dataclass(frozen=True, init=False, eq=False, repr=False)
class MatchedStage2Service(ChallengeStageService):
    model: ModelId

    def __init__(self, model: ModelId, transport=None):
        if model not in MODELS:
            raise ValueError('Select a frozen matched model')
        # Frozen instance, including model identity; no shared MODEL replacement.
        object.__setattr__(self, 'model', model)
        object.__setattr__(self, '_transport', transport)
        object.__setattr__(self, 'config', CONFIG)
        object.__setattr__(self, 'instructions', STAGE2_INSTRUCTIONS)
        object.__setattr__(self, 'parse_content', parse_stage2)

    async def _request(self, context: ReasoningContext, key: str) -> ChallengeStage2:
        # Historical request/parser path, changing only explicit model selection
        # and omitting reasoning_budget for BOTH matched models.
        payload = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": self.instructions},
                         {"role": "user", "content": context.model_dump_json()}],
            "temperature": self.config.temperature, "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "reasoning_effort": self.config.reasoning_effort,
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ReasoningFailed(failure=ProviderFailure(failure_kind="request_size")) from None
        async with httpx.AsyncClient(transport=self._transport, timeout=PROVIDER_TIMEOUT_SECONDS,
                                     follow_redirects=False) as client:
            async with client.stream("POST", ENDPOINT, content=encoded, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Accept": "application/json",
            }) as response:
                if response.status_code != 200:
                    raise ReasoningFailed(failure=ProviderFailure(
                        failure_kind="http_status", http_status=response.status_code)) from None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise InvalidDecision(invalid_reason="response_size") from None
                    body.extend(chunk)
        try:
            envelope = strict_json(body.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("Invalid envelope")
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Invalid choices")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("Invalid choice")
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Invalid message")
            if choice["finish_reason"] != "stop":
                raise InvalidDecision(invalid_reason="finish_reason") from None
            if message.get("role") != "assistant":
                raise ValueError("Invalid message role")
            if message.get("tool_calls") or message.get("function_call"):
                raise InvalidDecision(invalid_reason="tool_or_function_call") from None
            if message.get("refusal"):
                raise InvalidDecision(invalid_reason="refusal") from None
            # Only final content; never expose/use separate provider reasoning.
            return self.parse_content(message["content"])
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise InvalidDecision(invalid_reason="response_envelope") from None

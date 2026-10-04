"""Development-only fixed Ultra Stage 2; historical providers remain unchanged."""
import json

import httpx

from app.reasoning import (InvalidDecision, ProviderFailure, ReasoningContext,
                           ReasoningFailed, strict_json)
from evals.interviewer.two_stage_challenge_contract import ChallengeStage2
from evals.interviewer.two_stage_challenge_provider import ChallengeStageService
from evals.interviewer.two_stage_provider import (ENDPOINT, MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES, MODEL as STAGE1_MODEL, PROVIDER_TIMEOUT_SECONDS)

EXPERIMENT_VERSION = 'interviewer-two-stage-challenge-ultra-v1'
STAGE2_MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'


class UltraStageService(ChallengeStageService):
    @property
    def model(self):
        """Fixed read-only identity; no runtime selector or shared global switching."""
        return STAGE2_MODEL

    async def _request(self, context: ReasoningContext, key: str) -> ChallengeStage2:
        # Identical historical request/envelope body except for this model value.
        payload = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": self.instructions},
                         {"role": "user", "content": context.model_dump_json()}],
            "temperature": self.config.temperature, "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "reasoning_effort": self.config.reasoning_effort,
            "reasoning_budget": self.config.reasoning_budget,
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
            # Only content is parsed; provider reasoning fields are never returned or stored.
            return self.parse_content(message["content"])
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise InvalidDecision(invalid_reason="response_envelope") from None

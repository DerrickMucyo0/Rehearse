"""NVIDIA transport for semantic diagnosis, returning only raw final content.

The prompt builder owns semantic input; the existing JSON boundary owns output
validation. This client performs one request without retries or state changes.
"""

import os

import httpx

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis_failure import (
    SemanticDiagnosisFailureCategory,
    validate_failure_metadata,
)
from app.semantic_diagnosis_prompt import build_semantic_diagnosis_prompt

NVIDIA_SEMANTIC_DIAGNOSIS_ENDPOINT = (
    "https://integrate.api.nvidia.com/v1/chat/completions"
)
NVIDIA_SEMANTIC_DIAGNOSIS_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
NVIDIA_SEMANTIC_DIAGNOSIS_TIMEOUT_SECONDS = 60
NVIDIA_SEMANTIC_DIAGNOSIS_MAX_TOKENS = 8192


class NVIDIASemanticDiagnosisUnavailable(RuntimeError):
    pass


class NVIDIASemanticDiagnosisTimeout(RuntimeError):
    pass


class NVIDIASemanticDiagnosisFailed(RuntimeError):
    """Fixed provider error with only a safe category and optional HTTP status."""

    __slots__ = ("category", "upstream_status")

    def __init__(
        self,
        category: SemanticDiagnosisFailureCategory,
        upstream_status: int | None = None,
    ) -> None:
        validate_failure_metadata(category, upstream_status)
        if category not in (
            "provider_http_error",
            "provider_transport_error",
            "provider_response_contract_error",
        ):
            raise ValueError("Invalid semantic diagnosis provider failure category.") from None
        super().__init__("Semantic diagnosis provider request failed.")
        self.category = category
        self.upstream_status = upstream_status


def _assistant_content(response: httpx.Response) -> str:
    """Require one complete final message without interpreting semantic JSON."""
    if not response.is_success:
        raise NVIDIASemanticDiagnosisFailed(
            "provider_http_error", response.status_code,
        ) from None
    envelope = None
    try:
        envelope = response.json()
    except ValueError:
        pass
    # Validate and raise outside the handler so JSON failure details are not
    # retained in the public exception's implicit context.
    choices = envelope.get("choices") if isinstance(envelope, dict) else None
    if isinstance(choices, list) and len(choices) == 1:
        choice = choices[0]
        if isinstance(choice, dict) and choice.get("finish_reason") == "stop":
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content != "":
                    return content
    raise NVIDIASemanticDiagnosisFailed(
        "provider_response_contract_error",
    ) from None


class NVIDIANemotronSemanticDiagnosisClient:
    """Request-time configuration and injected HTTP transport; no semantic policy."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport

    async def request(
        self,
        context: DiagnosisContext,
    ) -> str:
        key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not key:
            raise NVIDIASemanticDiagnosisUnavailable(
                "Semantic diagnosis provider is not configured."
            ) from None
        prompt = build_semantic_diagnosis_prompt(context)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=NVIDIA_SEMANTIC_DIAGNOSIS_TIMEOUT_SECONDS,
            ) as http:
                response = await http.post(
                    NVIDIA_SEMANTIC_DIAGNOSIS_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Accept": "application/json",
                    },
                    json={
                        "model": NVIDIA_SEMANTIC_DIAGNOSIS_MODEL,
                        "messages": [
                            {"role": "system", "content": prompt.system},
                            {"role": "user", "content": prompt.user},
                        ],
                        "temperature": 0.0,
                        "max_tokens": NVIDIA_SEMANTIC_DIAGNOSIS_MAX_TOKENS,
                        "stream": False,
                        "response_format": {"type": "json_object"},
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
        except (httpx.TimeoutException, TimeoutError):
            timed_out = True
        except httpx.RequestError:
            timed_out = False
        else:
            return _assistant_content(response)
        # Leaving the handlers also discards implicit sensitive exception context.
        if timed_out:
            raise NVIDIASemanticDiagnosisTimeout(
                "Semantic diagnosis provider timed out."
            ) from None
        raise NVIDIASemanticDiagnosisFailed(
            "provider_transport_error",
        ) from None

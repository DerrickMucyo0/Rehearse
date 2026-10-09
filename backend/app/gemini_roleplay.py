"""One stateless Gemini Interactions API request for a bounded next question."""

import asyncio
import os

import httpx

from app.roleplay import RoleplayContext, RoleplayUnavailable
from app.roleplay_prompt import build_roleplay_prompt

GEMINI_INTERACTIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_ROLEPLAY_MODEL = "gemini-3.5-flash"
GEMINI_CONNECT_TIMEOUT_SECONDS = 15
GEMINI_READ_TIMEOUT_SECONDS = 60
GEMINI_WRITE_TIMEOUT_SECONDS = 15
GEMINI_POOL_TIMEOUT_SECONDS = 15
GEMINI_TOTAL_TIMEOUT_SECONDS = 75


def _interaction_text(response: httpx.Response) -> str:
    if not response.is_success:
        raise RoleplayUnavailable("http_status", response.status_code) from None

    payload = None
    try:
        payload = response.json()
    except ValueError:
        pass
    if not isinstance(payload, dict):
        raise RoleplayUnavailable("response_contract") from None

    output_text = payload.get("output_text")
    if type(output_text) is str and output_text.strip():
        return output_text.strip()

    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            content = step.get("content")
            if type(content) is str and content.strip():
                return content.strip()
            if isinstance(content, list):
                text_parts = [
                    part["text"].strip()
                    for part in content
                    if isinstance(part, dict)
                    and type(part.get("text")) is str
                    and part["text"].strip()
                ]
                if text_parts:
                    return "\n".join(text_parts)
    raise RoleplayUnavailable("response_contract") from None


class GeminiRoleplayClient:
    """Calls Gemini once; no provider-side state or retries are used."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def request(self, context: RoleplayContext) -> str:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RoleplayUnavailable("unavailable") from None

        prompt = build_roleplay_prompt(context)
        schema = {
            "type": "object",
            "properties": {
                "roleplay_version": {
                    "type": "string",
                    "enum": ["live-ai-roleplay-v1"],
                },
                "next_question": {"type": "string"},
            },
            "required": ["roleplay_version", "next_question"],
            "additionalProperties": False,
        }
        model = os.environ.get("GEMINI_MODEL", GEMINI_ROLEPLAY_MODEL).strip()
        if not model:
            model = GEMINI_ROLEPLAY_MODEL

        try:
            async with asyncio.timeout(GEMINI_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=httpx.Timeout(
                        connect=GEMINI_CONNECT_TIMEOUT_SECONDS,
                        read=GEMINI_READ_TIMEOUT_SECONDS,
                        write=GEMINI_WRITE_TIMEOUT_SECONDS,
                        pool=GEMINI_POOL_TIMEOUT_SECONDS,
                    ),
                ) as http:
                    response = await http.post(
                        GEMINI_INTERACTIONS_ENDPOINT,
                        headers={
                            "x-goog-api-key": key,
                            "Accept": "application/json",
                        },
                        json={
                            "model": model,
                            "system_instruction": prompt.system,
                            "input": prompt.user,
                            "store": False,
                            "response_format": {
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": schema,
                            },
                        },
                    )
        except (httpx.TimeoutException, TimeoutError):
            failure_kind = "timeout"
        except httpx.RequestError:
            failure_kind = "transport"
        else:
            return _interaction_text(response)
        raise RoleplayUnavailable(failure_kind) from None

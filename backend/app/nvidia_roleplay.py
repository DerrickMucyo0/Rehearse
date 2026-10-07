"""One NVIDIA request for final roleplay JSON, independent of diagnosis."""

import asyncio
import os

import httpx

from app.roleplay import RoleplayContext, RoleplayUnavailable
from app.roleplay_prompt import build_roleplay_prompt

NVIDIA_ROLEPLAY_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_ROLEPLAY_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
NVIDIA_ROLEPLAY_CONNECT_TIMEOUT_SECONDS = 60
NVIDIA_ROLEPLAY_READ_TIMEOUT_SECONDS = None
NVIDIA_ROLEPLAY_WRITE_TIMEOUT_SECONDS = 60
NVIDIA_ROLEPLAY_POOL_TIMEOUT_SECONDS = 60
NVIDIA_ROLEPLAY_TOTAL_TIMEOUT_SECONDS = 120
NVIDIA_ROLEPLAY_MAX_TOKENS = 8192


def _assistant_content(response: httpx.Response) -> str:
    if not response.is_success:
        raise RoleplayUnavailable("http_status", response.status_code) from None
    envelope = None
    try:
        envelope = response.json()
    except ValueError:
        pass
    choices = envelope.get("choices") if isinstance(envelope, dict) else None
    if isinstance(choices, list) and len(choices) == 1:
        choice = choices[0]
        if isinstance(choice, dict) and choice.get("finish_reason") == "stop":
            message = choice.get("message")
            if (
                isinstance(message, dict)
                and not message.get("tool_calls") and not message.get("function_call")
                and not message.get("refusal")
            ):
                content = message.get("content")
                if type(content) is str and content:
                    return content
    raise RoleplayUnavailable("response_contract") from None


class NVIDIANemotronRoleplayClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def request(self, context: RoleplayContext) -> str:
        key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not key:
            raise RoleplayUnavailable("unavailable") from None
        prompt = build_roleplay_prompt(context)
        try:
            async with asyncio.timeout(NVIDIA_ROLEPLAY_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=httpx.Timeout(
                        connect=NVIDIA_ROLEPLAY_CONNECT_TIMEOUT_SECONDS,
                        read=NVIDIA_ROLEPLAY_READ_TIMEOUT_SECONDS,
                        write=NVIDIA_ROLEPLAY_WRITE_TIMEOUT_SECONDS,
                        pool=NVIDIA_ROLEPLAY_POOL_TIMEOUT_SECONDS,
                    ),
                ) as http:
                    response = await http.post(
                        NVIDIA_ROLEPLAY_ENDPOINT,
                        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                        json={
                            "model": NVIDIA_ROLEPLAY_MODEL,
                            "messages": [
                                {"role": "system", "content": prompt.system},
                                {"role": "user", "content": prompt.user},
                            ],
                            "temperature": 0.0,
                            "max_tokens": NVIDIA_ROLEPLAY_MAX_TOKENS,
                            "stream": False,
                            "response_format": {"type": "json_object"},
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    )
        except (httpx.TimeoutException, TimeoutError):
            failure_kind = "timeout"
        except httpx.RequestError:
            failure_kind = "transport"
        else:
            return _assistant_content(response)
        raise RoleplayUnavailable(failure_kind) from None

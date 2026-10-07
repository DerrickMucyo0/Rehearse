"""Raw provider client seam and roleplay-specific strict output adapter."""

from typing import Protocol

from app.roleplay import (
    RoleplayContext, RoleplayQuestion, RoleplayUnavailable, _safe_failure,
    parse_roleplay_question_json,
)


class RoleplayJSONClient(Protocol):
    async def request(self, context: RoleplayContext) -> str:
        ...


class JSONRoleplayAdapter:
    def __init__(self, client: RoleplayJSONClient) -> None:
        self._client = client

    async def generate(self, context: RoleplayContext) -> RoleplayQuestion:
        if type(context) is not RoleplayContext:
            raise RoleplayUnavailable("adapter_error") from None
        try:
            payload = await self._client.request(context)
            return parse_roleplay_question_json(payload)
        except Exception as error:
            failure_kind, http_status = _safe_failure(error)
        raise RoleplayUnavailable(failure_kind, http_status) from None

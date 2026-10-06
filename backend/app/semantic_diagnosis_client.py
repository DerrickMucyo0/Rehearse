"""Provider-neutral raw-output client seam and strict JSON adapter.

The injected client owns the request. The existing parser owns validation.
This module only forwards supplied objects between those two boundaries.
"""

from typing import Protocol

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_json import parse_semantic_diagnosis_json


class SemanticDiagnosisJSONClient(Protocol):
    async def request(
        self,
        context: DiagnosisContext,
    ) -> str:
        ...


class JSONSemanticDiagnosisAdapter:
    """Request once and return the exact result from the existing JSON parser."""

    def __init__(self, client: SemanticDiagnosisJSONClient) -> None:
        self._client = client

    async def diagnose(
        self,
        context: DiagnosisContext,
    ) -> SemanticDiagnosis:
        payload = await self._client.request(context)
        return parse_semantic_diagnosis_json(payload)

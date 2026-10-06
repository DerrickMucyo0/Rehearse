"""Provider-neutral orchestration after an authoritative context read returns.

The reader owns persistence; the existing adapter boundary owns semantic output
validation. This module only sequences those boundaries and preserves objects.
"""

from typing import Protocol
from uuid import UUID

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapter, request_semantic_diagnosis


class DiagnosisContextReader(Protocol):
    def get_diagnosis_context(
        self,
        session_id: UUID,
        question_index: int,
        attempt_number: int,
    ) -> DiagnosisContext:
        ...


async def diagnose_context(
    adapter: SemanticDiagnosisAdapter,
    context: DiagnosisContext,
) -> tuple[DiagnosisContext, SemanticDiagnosis]:
    diagnosis = await request_semantic_diagnosis(adapter, context)
    return context, diagnosis


async def diagnose_persisted_attempt(
    reader: DiagnosisContextReader,
    adapter: SemanticDiagnosisAdapter,
    *,
    session_id: UUID,
    question_index: int,
    attempt_number: int,
) -> tuple[DiagnosisContext, SemanticDiagnosis]:
    """Read once, then request once, returning the exact supplied objects."""
    context = reader.get_diagnosis_context(session_id, question_index, attempt_number)
    return await diagnose_context(adapter, context)

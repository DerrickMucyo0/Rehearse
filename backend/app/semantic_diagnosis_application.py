"""Normalize known generation failures without changing existing orchestration."""

from uuid import UUID

from app.diagnosis import DiagnosisContext
from app.diagnosis_orchestration import (
    DiagnosisContextReader,
    diagnose_context,
    diagnose_persisted_attempt,
)
from app.nvidia_semantic_diagnosis import (
    NVIDIASemanticDiagnosisFailed,
    NVIDIASemanticDiagnosisTimeout,
    NVIDIASemanticDiagnosisUnavailable,
)
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import (
    SemanticDiagnosisAdapter,
    SemanticDiagnosisAdapterContractError,
)
from app.semantic_diagnosis_json import SemanticDiagnosisJSONContractError


class SemanticDiagnosisUnavailable(RuntimeError):
    pass


class SemanticDiagnosisTimeout(RuntimeError):
    pass


class SemanticDiagnosisFailed(RuntimeError):
    pass


async def diagnose_application_attempt(
    reader: DiagnosisContextReader,
    adapter: SemanticDiagnosisAdapter,
    *,
    session_id: UUID,
    question_index: int,
    attempt_number: int,
) -> tuple[DiagnosisContext, SemanticDiagnosis]:
    """Delegate once and translate only known failures into fixed public errors."""
    try:
        return await diagnose_persisted_attempt(
            reader,
            adapter,
            session_id=session_id,
            question_index=question_index,
            attempt_number=attempt_number,
        )
    except NVIDIASemanticDiagnosisUnavailable:
        failure = "unavailable"
    except NVIDIASemanticDiagnosisTimeout:
        failure = "timeout"
    except (
        NVIDIASemanticDiagnosisFailed,
        SemanticDiagnosisJSONContractError,
        SemanticDiagnosisAdapterContractError,
    ):
        failure = "failed"
    # Raise after leaving the handlers so private exception context is discarded.
    if failure == "unavailable":
        raise SemanticDiagnosisUnavailable("Semantic diagnosis is not configured.") from None
    if failure == "timeout":
        raise SemanticDiagnosisTimeout("Semantic diagnosis timed out.") from None
    raise SemanticDiagnosisFailed("Unable to generate semantic diagnosis.") from None


async def diagnose_application_context(
    adapter: SemanticDiagnosisAdapter,
    context: DiagnosisContext,
) -> tuple[DiagnosisContext, SemanticDiagnosis]:
    """Delegate a supplied context once and translate only known failures."""
    try:
        return await diagnose_context(adapter, context)
    except NVIDIASemanticDiagnosisUnavailable:
        failure = "unavailable"
    except NVIDIASemanticDiagnosisTimeout:
        failure = "timeout"
    except (
        NVIDIASemanticDiagnosisFailed,
        SemanticDiagnosisJSONContractError,
        SemanticDiagnosisAdapterContractError,
    ):
        failure = "failed"
    # Raise after leaving the handlers so private exception context is discarded.
    if failure == "unavailable":
        raise SemanticDiagnosisUnavailable("Semantic diagnosis is not configured.") from None
    if failure == "timeout":
        raise SemanticDiagnosisTimeout("Semantic diagnosis timed out.") from None
    raise SemanticDiagnosisFailed("Unable to generate semantic diagnosis.") from None

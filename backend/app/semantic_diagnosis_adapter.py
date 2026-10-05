"""Provider-neutral semantic adapter boundary and sequential execution harness.

The injected adapter owns reasoning. This module only checks the input/output
types and preserves supplied objects without judgment or external side effects.
"""

from collections.abc import Sequence
from typing import Protocol

from app.diagnosis import DiagnosisContext
from app.semantic_diagnosis import SemanticDiagnosis


class SemanticDiagnosisAdapter(Protocol):
    async def diagnose(self, context: DiagnosisContext) -> SemanticDiagnosis:
        ...


class SemanticDiagnosisAdapterContractError(RuntimeError):
    """Adapter returned a value outside the SemanticDiagnosis contract."""


async def request_semantic_diagnosis(
    adapter: SemanticDiagnosisAdapter,
    context: DiagnosisContext,
) -> SemanticDiagnosis:
    """Request once, preserving context, result, and adapter exceptions."""
    if not isinstance(context, DiagnosisContext):
        raise TypeError("Semantic diagnosis context must be a DiagnosisContext instance.")
    result = await adapter.diagnose(context)
    if not isinstance(result, SemanticDiagnosis):
        raise SemanticDiagnosisAdapterContractError("Adapter must return a SemanticDiagnosis instance.")
    return result


async def run_semantic_diagnosis_eval(
    adapter: SemanticDiagnosisAdapter,
    contexts: Sequence[DiagnosisContext],
) -> tuple[SemanticDiagnosis, ...]:
    """Execute in input order, stopping at the first failure without scoring."""
    results: list[SemanticDiagnosis] = []
    for context in contexts:
        results.append(await request_semantic_diagnosis(adapter, context))
    return tuple(results)

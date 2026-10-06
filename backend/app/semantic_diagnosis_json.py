"""Provider-neutral JSON boundary using the authoritative semantic contract."""

from pydantic import ValidationError

from app.semantic_diagnosis import SemanticDiagnosis


class SemanticDiagnosisJSONContractError(RuntimeError):
    """Raw semantic output did not satisfy the SemanticDiagnosis contract."""


def parse_semantic_diagnosis_json(payload: str) -> SemanticDiagnosis:
    """Validate supplied JSON without coercion, repair, or feedback rewriting."""
    if not isinstance(payload, str):
        raise TypeError("Semantic diagnosis JSON payload must be a string.")
    try:
        return SemanticDiagnosis.model_validate_json(payload)
    except ValidationError:
        pass
    # Leave the handler before raising so validation details are not retained.
    raise SemanticDiagnosisJSONContractError(
        "Semantic diagnosis output did not match the required contract."
    ) from None

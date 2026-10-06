"""Concrete dependency composition behind the provider-neutral adapter contract."""

from app.nvidia_semantic_diagnosis import NVIDIANemotronSemanticDiagnosisClient
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapter
from app.semantic_diagnosis_client import JSONSemanticDiagnosisAdapter


def get_semantic_diagnosis_adapter() -> SemanticDiagnosisAdapter:
    return JSONSemanticDiagnosisAdapter(
        NVIDIANemotronSemanticDiagnosisClient()
    )

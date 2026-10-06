"""Closed, content-free diagnostic metadata for semantic diagnosis failures."""

from typing import Literal, get_args


SemanticDiagnosisFailureCategory = Literal[
    "provider_http_error",
    "provider_transport_error",
    "provider_response_contract_error",
    "semantic_json_contract_error",
    "adapter_contract_error",
]
_CATEGORIES = get_args(SemanticDiagnosisFailureCategory)


def validate_failure_metadata(
    category: SemanticDiagnosisFailureCategory,
    upstream_status: int | None,
) -> None:
    """Accept only allowlisted built-in primitives, without formatting input."""
    if (
        type(category) is not str
        or category not in _CATEGORIES
        or (
            upstream_status is not None
            and (
                type(upstream_status) is not int
                or category != "provider_http_error"
            )
        )
    ):
        raise ValueError("Invalid semantic diagnosis failure metadata.") from None

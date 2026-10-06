"""Offline checks of curated constraints against hand-authored diagnoses.

The examples below are synthetic evaluator fixtures, not model-quality evidence
or a universal rubric for assessing interview answers.
"""

import ast
import inspect
import itertools
import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from app.comparisons import (
    ComparisonMetrics,
    DeliveryComparison,
    DeliveryMetricChange,
    MeasurementSnapshot,
    MetricChange,
)
from app.diagnosis import DiagnosisContext, PreviousAttemptFacts
from app.semantic_diagnosis import (
    AddressedQuestion,
    NextFocus,
    SemanticDiagnosis,
    StructureAssessment,
)
from app import semantic_diagnosis_eval as evaluator
from app.semantic_diagnosis_eval import (
    EVAL_EXPECTATION_VERSION,
    SemanticDiagnosisExpectation,
    SemanticEvalViolation,
    SemanticEvalViolationCode,
    evaluate_semantic_diagnosis,
)


EXPECTATION_FIELDS = (
    "expectation_version", "allowed_addressed_question", "allowed_structure",
    "allowed_next_focus", "strengths_requirement", "missing_information_requirement",
)
ENUM_DIMENSIONS = (
    ("allowed_addressed_question", "addressed_question", AddressedQuestion,
     "addressed_question_not_allowed"),
    ("allowed_structure", "structure", StructureAssessment, "structure_not_allowed"),
    ("allowed_next_focus", "next_focus", NextFocus, "next_focus_not_allowed"),
)
SET_FIELDS = tuple(item[0] for item in ENUM_DIMENSIONS)
REQUIREMENT_FIELDS = ("strengths_requirement", "missing_information_requirement")
REQUIREMENTS = ("any", "empty", "nonempty")
VIOLATION_CODES = (
    "addressed_question_not_allowed", "structure_not_allowed", "next_focus_not_allowed",
    "strengths_must_be_empty", "strengths_must_be_nonempty",
    "missing_information_must_be_empty", "missing_information_must_be_nonempty",
)
TYPE_ERRORS = (
    "Semantic evaluation context must be a DiagnosisContext instance.",
    "Semantic evaluation diagnosis must be a SemanticDiagnosis instance.",
    "Semantic evaluation expectation must be a SemanticDiagnosisExpectation instance.",
)
FORBIDDEN_FIELDS = (
    "id", "session_id", "attempt_id", "attempt_number", "question_index",
    "measurement_id", "measurement_version", "measurement_source",
    "recognized_word_count", "um_count", "uh_count", "timed_utterance_span_seconds",
    "estimated_words_per_minute", "pause_count", "total_pause_duration_seconds",
    "longest_pause_seconds", "measurement", "previous_attempt", "comparison",
    "delivery_metrics", "score", "scores", "confidence", "metadata", "provider_payload",
    "question", "answer", "transcript", "objective_metrics", "state", "session_state",
    "current_question_index", "next_question", "retry_question", "should_retry",
    "should_advance", "advance_to_next_question", "completed", "unexpected_field",
)
MODEL_METHODS = (
    "__init__", "model_validate", "model_validate_json", "model_validate_strings",
    "model_construct", "model_copy", "model_dump", "model_dump_json",
)


def context(*, question="Describe a problem you solved.", answer="Synthetic answer.", **updates):
    values = {
        "question": question, "answer": answer, "question_index": 0,
        "attempt_number": 1, "measurement": None, "previous_attempt": None,
    }
    values.update(updates)
    return DiagnosisContext(**values)


def diagnosis(**updates):
    """Supply arbitrary valid feedback; the evaluator does not generate it."""
    values = {
        "addressed_question": "partially",
        "addressed_question_reason": "Synthetic explanation of the supplied assessment.",
        "strengths": ("Synthetic strength.",),
        "missing_information": ("Synthetic missing detail.",),
        "structure": "mixed",
        "structure_feedback": "Synthetic explanation of the supplied structure.",
        "next_focus": "supporting_detail",
        "next_focus_reason": "Synthetic explanation of the supplied focus.",
        "retry_instruction": "Synthetic instruction for another attempt.",
    }
    values.update(updates)
    return SemanticDiagnosis(**values)


def codes(violations):
    assert type(violations) is tuple
    assert all(type(item) is SemanticEvalViolation for item in violations)
    return tuple(item.code for item in violations)


def schema_property_names(value):
    if isinstance(value, dict):
        yield from value.get("properties", {})
        for child in value.values():
            yield from schema_property_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from schema_property_names(child)


def curated_case(category):
    """Four local categories with explicitly scoped, hand-chosen constraints."""
    if category == "empty_answer":
        supplied = context(answer="")
        expected = SemanticDiagnosisExpectation(
            allowed_addressed_question=frozenset({"no"}),
            allowed_structure=frozenset({"insufficient_content"}),
            allowed_next_focus=frozenset({"answer_the_question"}),
            strengths_requirement="empty", missing_information_requirement="nonempty",
        )
        candidates = (diagnosis(
            addressed_question="no", structure="insufficient_content",
            next_focus="answer_the_question", strengths=(),
            missing_information=("Describe the problem and how you approached it.",),
        ),)
    elif category == "partial_situation_action":
        supplied = context(
            question="Describe a situation, the action you took, and the result.",
            answer=(
                "A release was blocked by failing checks. I isolated the failure and "
                "updated the checks with the team."
            ),
        )
        expected = SemanticDiagnosisExpectation(
            allowed_addressed_question=frozenset({"yes", "partially"}),
            allowed_structure=frozenset({"clear", "mixed"}),
            allowed_next_focus=frozenset({"completeness", "supporting_detail"}),
            strengths_requirement="nonempty", missing_information_requirement="nonempty",
        )
        candidates = tuple(diagnosis(
            addressed_question=addressed, structure=structure, next_focus=focus,
            strengths=("You describe the problem and your action.",),
            missing_information=("Explain the outcome of the updated checks.",),
        ) for addressed, structure, focus in itertools.product(
            ("yes", "partially"), ("clear", "mixed"), ("completeness", "supporting_detail"),
        ))
    elif category == "strong_concise_sar":
        supplied = context(
            question="Describe a situation, the action you took, and the result.",
            answer=(
                "A release was blocked by a flaky check. I reproduced the failure, "
                "fixed the setup, and reran the suite. The release shipped that day "
                "and the check remained stable."
            ),
        )
        expected = SemanticDiagnosisExpectation(
            allowed_addressed_question=frozenset({"yes"}),
            allowed_structure=frozenset({"clear"}),
            allowed_next_focus=frozenset({"maintain_strengths"}),
            strengths_requirement="nonempty", missing_information_requirement="empty",
        )
        candidates = (diagnosis(
            addressed_question="yes", structure="clear", next_focus="maintain_strengths",
            strengths=("You give a concise situation, action, and result.",),
            missing_information=(),
        ),)
    elif category == "substantive_disorganized":
        supplied = context(answer=(
            "The release shipped the same day. The check had failed. I worked "
            "with the team and fixed the setup after reproducing the failure. "
            "It stayed stable. That was what had blocked the release."
        ))
        expected = SemanticDiagnosisExpectation(allowed_structure=frozenset({"mixed", "unclear"}))
        candidates = tuple(diagnosis(
            structure=structure, addressed_question=addressed, next_focus=focus,
            strengths=strengths, missing_information=missing,
        ) for structure, addressed, focus, strengths, missing in (
            ("mixed", "yes", "structure", ("The example has substantive details.",), ()),
            ("unclear", "partially", "completeness", (), ("Synthetic detail.",)),
            ("mixed", "no", "maintain_strengths", (), ()),
        ))
    else:
        raise AssertionError(f"Unknown synthetic category: {category}")
    return supplied, expected, candidates


def test_public_version_literals_defaults_and_signature_are_exact():
    assert EVAL_EXPECTATION_VERSION == "semantic-eval-expectation-v1"
    assert get_args(SemanticEvalViolationCode) == VIOLATION_CODES
    expected = SemanticDiagnosisExpectation()
    assert expected.model_dump() == {
        "expectation_version": EVAL_EXPECTATION_VERSION,
        "allowed_addressed_question": None, "allowed_structure": None,
        "allowed_next_focus": None, "strengths_requirement": "any",
        "missing_information_requirement": "any",
    }
    assert SemanticDiagnosisExpectation(expectation_version=EVAL_EXPECTATION_VERSION) == expected
    assert not inspect.iscoroutinefunction(evaluate_semantic_diagnosis)
    signature = inspect.signature(evaluate_semantic_diagnosis)
    assert tuple(signature.parameters) == ("context", "diagnosis", "expectation")
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
    assert get_type_hints(evaluate_semantic_diagnosis) == {
        "context": DiagnosisContext, "diagnosis": SemanticDiagnosis,
        "expectation": SemanticDiagnosisExpectation,
        "return": tuple[SemanticEvalViolation, ...],
    }
    with pytest.raises(TypeError):
        evaluate_semantic_diagnosis(context(), diagnosis(), expected)


@pytest.mark.parametrize("value", [
    "semantic-eval-expectation-v2", "semantic-eval-expectation-v1 ", "", None,
    True, 1, 1.0, b"semantic-eval-expectation-v1", {}, [],
])
def test_wrong_or_non_string_expectation_version_is_rejected(value):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(expectation_version=value)


@pytest.mark.parametrize(("field", "diagnosis_field", "alias", "code"), ENUM_DIMENSIONS)
def test_none_singleton_and_multiple_enum_alternatives_preserve_strict_frozensets(
    field, diagnosis_field, alias, code,
):
    assert getattr(SemanticDiagnosisExpectation(**{field: None}), field) is None
    for members in ((value,) for value in get_args(alias)):
        selected = frozenset(members)
        result = SemanticDiagnosisExpectation(**{field: selected})
        assert type(getattr(result, field)) is frozenset
        assert getattr(result, field) == selected
    alternatives = frozenset(get_args(alias))
    result = SemanticDiagnosisExpectation(**{field: alternatives})
    decoded = SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: list(alternatives)}))
    assert result == decoded
    assert type(getattr(decoded, field)) is frozenset
    assert getattr(decoded, field) == alternatives
    with pytest.raises(AttributeError):
        getattr(decoded, field).add(next(iter(alternatives)))


@pytest.mark.parametrize("field", SET_FIELDS)
def test_empty_allowlists_are_rejected_in_python_and_json(field):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(**{field: frozenset()})
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: []}))


@pytest.mark.parametrize("field", SET_FIELDS)
@pytest.mark.parametrize("value", [
    [], ["yes"], (), ("yes",), set(), {"yes"}, "yes", b"yes",
    True, 1, 1.5, {}, {"item": "yes"},
])
def test_python_allowlists_reject_non_frozenset_containers_and_scalars(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(**{field: value})


@pytest.mark.parametrize("field", SET_FIELDS)
def test_python_allowlists_reject_iterators(field):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(**{field: iter(("yes",))})


@pytest.mark.parametrize("field", SET_FIELDS)
@pytest.mark.parametrize("value", ["unknown", "YES", "yes ", "", None, True, 1, 1.0, b"yes"])
def test_allowlist_members_reject_unknown_enum_values_and_scalar_coercion(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(**{field: frozenset({value})})
    if not isinstance(value, bytes):
        with pytest.raises(ValidationError):
            SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: [value]}))


@pytest.mark.parametrize("field", SET_FIELDS)
@pytest.mark.parametrize("value", ["yes", True, 1, 1.5, {}, {"item": "yes"}])
def test_json_allowlists_require_arrays_or_null(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: value}))
    assert getattr(SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: None})), field) is None


@pytest.mark.parametrize(("field", "diagnosis_field", "alias", "code"), ENUM_DIMENSIONS)
def test_json_allowlists_deduplicate_without_removing_valid_alternatives(field, diagnosis_field, alias, code):
    values = get_args(alias)
    result = SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: [*values, values[0]]}))
    assert getattr(result, field) == frozenset(values)


@pytest.mark.parametrize("field", REQUIREMENT_FIELDS)
@pytest.mark.parametrize("value", REQUIREMENTS)
def test_each_collection_requirement_is_accepted_exactly(field, value):
    expected = SemanticDiagnosisExpectation(**{field: value})
    assert getattr(expected, field) == value
    assert SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: value})) == expected


@pytest.mark.parametrize("field", REQUIREMENT_FIELDS)
@pytest.mark.parametrize("value", ["unknown", "ANY", "empty ", "", None, True, 1, b"any", [], {}])
def test_collection_requirements_reject_unknown_values_and_non_strings(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosisExpectation(**{field: value})
    if not isinstance(value, bytes):
        with pytest.raises(ValidationError):
            SemanticDiagnosisExpectation.model_validate_json(json.dumps({field: value}))


@pytest.mark.parametrize(("model", "field"), [
    *((SemanticDiagnosisExpectation, name) for name in EXPECTATION_FIELDS),
    (SemanticEvalViolation, "code"),
])
def test_models_are_strict_frozen_and_forbid_extras(model, field):
    assert model.model_config["strict"] is True
    assert model.model_config["frozen"] is True
    assert model.model_config["extra"] == "forbid"
    value = model() if model is SemanticDiagnosisExpectation else model(code=VIOLATION_CODES[0])
    before = value.model_dump()
    with pytest.raises(ValidationError) as assignment:
        setattr(value, field, getattr(value, field))
    assert assignment.value.errors()[0]["type"] == "frozen_instance"
    with pytest.raises(ValidationError) as deletion:
        delattr(value, field)
    assert deletion.value.errors()[0]["type"] == "frozen_instance"
    assert value.model_dump() == before


@pytest.mark.parametrize("model", (SemanticDiagnosisExpectation, SemanticEvalViolation))
@pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
def test_identity_metadata_measurement_score_and_state_extras_are_forbidden(model, field):
    payload = {field: {"synthetic": "unexpected"}}
    if model is SemanticEvalViolation:
        payload["code"] = VIOLATION_CODES[0]
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert any(item["loc"] == (field,) and item["type"] == "extra_forbidden" for item in error.value.errors())
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("code", VIOLATION_CODES)
def test_violation_accepts_each_closed_code_and_has_only_a_code(code):
    result = SemanticEvalViolation(code=code)
    assert result.model_dump() == {"code": code}
    assert SemanticEvalViolation.model_validate_json(json.dumps({"code": code})) == result


@pytest.mark.parametrize("value", ["unknown", "score_low", "", None, True, 1, b"structure_not_allowed", [], {}])
def test_violation_rejects_unknown_or_non_string_codes(value):
    with pytest.raises(ValidationError):
        SemanticEvalViolation(code=value)
    if not isinstance(value, bytes):
        with pytest.raises(ValidationError):
            SemanticEvalViolation.model_validate_json(json.dumps({"code": value}))


def test_violation_code_is_required():
    with pytest.raises(ValidationError) as error:
        SemanticEvalViolation()
    assert error.value.errors()[0]["type"] == "missing"


def test_json_schemas_allow_only_the_exact_contract_fields():
    expected = SemanticDiagnosisExpectation.model_json_schema()
    violation = SemanticEvalViolation.model_json_schema()
    for model, schema, fields in (
        (SemanticDiagnosisExpectation, expected, EXPECTATION_FIELDS),
        (SemanticEvalViolation, violation, ("code",)),
    ):
        assert tuple(model.model_fields) == fields
        assert tuple(schema["properties"]) == fields
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema_property_names(schema)) == set(fields)
        assert set(schema_property_names(schema)).isdisjoint(FORBIDDEN_FIELDS)
    assert expected.get("required", []) == []
    version = expected["properties"]["expectation_version"]
    assert version["const"] == version["default"] == EVAL_EXPECTATION_VERSION
    for field, diagnosis_field, alias, code in ENUM_DIMENSIONS:
        prop = expected["properties"][field]
        assert prop["default"] is None
        branches = prop["anyOf"]
        assert len(branches) == 2
        arrays = [branch for branch in branches if branch.get("type") == "array"]
        assert len(arrays) == 1
        assert arrays[0]["uniqueItems"] is True
        assert arrays[0]["minItems"] == 1
        assert arrays[0]["items"]["type"] == "string"
        assert tuple(arrays[0]["items"]["enum"]) == get_args(alias)
        assert {"type": "null"} in branches
    for field in REQUIREMENT_FIELDS:
        prop = expected["properties"][field]
        assert prop["type"] == "string"
        assert tuple(prop["enum"]) == REQUIREMENTS
        assert prop["default"] == "any"
    assert violation["required"] == ["code"]
    assert violation["properties"]["code"]["type"] == "string"
    assert tuple(violation["properties"]["code"]["enum"]) == VIOLATION_CODES


@pytest.mark.parametrize("category", (
    "empty_answer", "partial_situation_action", "strong_concise_sar", "substantive_disorganized",
))
def test_four_curated_categories_accept_their_hand_authored_semantic_alternatives(category):
    supplied, expected, candidates = curated_case(category)
    for candidate in candidates:
        assert evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected) == ()
    if category == "empty_answer":
        assert supplied.answer == ""
        assert "problem" in supplied.question
    elif category == "partial_situation_action":
        assert all(dimension in supplied.question for dimension in ("situation", "action", "result"))
        assert len(candidates) == 8
        assert len(expected.allowed_addressed_question) == 2
        assert len(expected.allowed_next_focus) == 2
    elif category == "strong_concise_sar":
        assert candidates[0].next_focus == "maintain_strengths"
        assert candidates[0].missing_information == ()
        invented_criticism = diagnosis(
            addressed_question="yes", structure="clear", next_focus="maintain_strengths",
            missing_information=("Invented criticism in this synthetic candidate.",),
        )
        assert codes(evaluate_semantic_diagnosis(
            context=supplied, diagnosis=invented_criticism, expectation=expected,
        )) == ("missing_information_must_be_empty",)
    else:
        assert expected.allowed_addressed_question is expected.allowed_next_focus is None
        assert expected.strengths_requirement == expected.missing_information_requirement == "any"
        assert codes(evaluate_semantic_diagnosis(
            context=supplied, diagnosis=diagnosis(structure="clear"), expectation=expected,
        )) == ("structure_not_allowed",)


@pytest.mark.parametrize(("field", "diagnosis_field", "alias", "code"), ENUM_DIMENSIONS)
def test_every_enum_dimension_applies_only_explicit_membership(field, diagnosis_field, alias, code):
    supplied = context()
    values = get_args(alias)
    for selected in values:
        expected = SemanticDiagnosisExpectation(**{field: frozenset({selected})})
        for actual in values:
            result = evaluate_semantic_diagnosis(
                context=supplied, diagnosis=diagnosis(**{diagnosis_field: actual}), expectation=expected,
            )
            assert codes(result) == (() if actual == selected else (code,))
    alternatives = SemanticDiagnosisExpectation(**{field: frozenset(values)})
    for actual in values:
        candidate = diagnosis(**{diagnosis_field: actual})
        assert evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=alternatives) == ()
        assert evaluate_semantic_diagnosis(
            context=supplied, diagnosis=candidate, expectation=SemanticDiagnosisExpectation(**{field: None}),
        ) == ()


@pytest.mark.parametrize(("strengths_requirement", "missing_requirement"), tuple(itertools.product(REQUIREMENTS, repeat=2)))
@pytest.mark.parametrize(("strengths", "missing"), tuple(itertools.product(((), ("Synthetic item.",)), repeat=2)))
def test_all_collection_emptiness_and_requirement_combinations(
    strengths_requirement, missing_requirement, strengths, missing,
):
    expected = SemanticDiagnosisExpectation(
        strengths_requirement=strengths_requirement, missing_information_requirement=missing_requirement,
    )
    result = evaluate_semantic_diagnosis(
        context=context(), diagnosis=diagnosis(strengths=strengths, missing_information=missing),
        expectation=expected,
    )
    expected_codes = []
    for requirement, items, prefix in (
        (strengths_requirement, strengths, "strengths"),
        (missing_requirement, missing, "missing_information"),
    ):
        if requirement == "empty" and items:
            expected_codes.append(f"{prefix}_must_be_empty")
        elif requirement == "nonempty" and not items:
            expected_codes.append(f"{prefix}_must_be_nonempty")
    assert codes(result) == tuple(expected_codes)


@pytest.mark.parametrize("strengths_requirement", ("empty", "nonempty"))
@pytest.mark.parametrize("missing_requirement", ("empty", "nonempty"))
def test_multiple_violations_are_unique_immutable_and_in_exact_dimension_order(
    strengths_requirement, missing_requirement,
):
    expected = SemanticDiagnosisExpectation(
        allowed_addressed_question=frozenset({"yes"}), allowed_structure=frozenset({"clear"}),
        allowed_next_focus=frozenset({"maintain_strengths"}),
        strengths_requirement=strengths_requirement, missing_information_requirement=missing_requirement,
    )
    supplied = diagnosis(
        addressed_question="no", structure="unclear", next_focus="structure",
        strengths=("Synthetic strength.",) if strengths_requirement == "empty" else (),
        missing_information=("Synthetic missing detail.",) if missing_requirement == "empty" else (),
    )
    result = evaluate_semantic_diagnosis(context=context(), diagnosis=supplied, expectation=expected)
    assert codes(result) == (
        "addressed_question_not_allowed", "structure_not_allowed", "next_focus_not_allowed",
        f"strengths_must_be_{strengths_requirement}",
        f"missing_information_must_be_{missing_requirement}",
    )
    assert len(set(codes(result))) == len(result) == 5
    with pytest.raises(TypeError):
        result[0] = result[1]
    for item in result:
        with pytest.raises(ValidationError):
            item.code = item.code


def block_model_field_access(monkeypatch, model, fields):
    original = model.__getattribute__

    def guarded(self, name):
        if name in fields:
            raise AssertionError(f"Evaluation must not read {model.__name__}.{name}.")
        return original(self, name)

    monkeypatch.setattr(model, "__getattribute__", guarded)


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("kind", ("none", "dict", "bool", "int", "str", "list", "class", "other_model"))
def test_non_instances_are_rejected_in_argument_order_before_any_field_access(monkeypatch, position, kind):
    inputs = [context(), diagnosis(), SemanticDiagnosisExpectation()]
    invalid = {
        "none": None, "dict": inputs[position].model_dump(), "bool": True, "int": 1,
        "str": "Synthetic impostor.", "list": [inputs[position]],
        "class": type(inputs[position]), "other_model": inputs[(position + 1) % 3],
    }[kind]
    inputs[position] = invalid
    # Later invalid inputs must not replace the first error or get inspected.
    for index in range(position + 1, 3):
        inputs[index] = None
    with monkeypatch.context() as guarded:
        for model in (DiagnosisContext, SemanticDiagnosis, SemanticDiagnosisExpectation):
            block_model_field_access(guarded, model, frozenset(model.model_fields))
        with pytest.raises(TypeError) as error:
            evaluate_semantic_diagnosis(context=inputs[0], diagnosis=inputs[1], expectation=inputs[2])
    assert str(error.value) == TYPE_ERRORS[position]


def test_all_types_are_checked_before_diagnosis_or_expectation_fields_are_read(monkeypatch):
    supplied, candidate = context(), diagnosis()
    with monkeypatch.context() as guarded:
        for model in (DiagnosisContext, SemanticDiagnosis):
            block_model_field_access(guarded, model, frozenset(model.model_fields))
        with pytest.raises(TypeError) as error:
            evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation={})
    assert str(error.value) == TYPE_ERRORS[2]


def test_evaluation_reads_no_context_facts_or_feedback_text_fields(monkeypatch):
    supplied = context(question="Do not inspect this question.", answer="Do not inspect this answer.")
    candidate = diagnosis()
    expected = SemanticDiagnosisExpectation(
        allowed_addressed_question=frozenset({"yes"}), allowed_structure=frozenset({"clear"}),
        allowed_next_focus=frozenset({"structure"}), strengths_requirement="empty",
        missing_information_requirement="empty",
    )
    text_fields = frozenset({
        "diagnosis_version", "addressed_question_reason", "structure_feedback",
        "next_focus_reason", "retry_instruction",
    })
    with monkeypatch.context() as guarded:
        block_model_field_access(guarded, DiagnosisContext, frozenset(DiagnosisContext.model_fields))
        block_model_field_access(guarded, SemanticDiagnosis, text_fields)
        block_model_field_access(guarded, SemanticDiagnosisExpectation, frozenset({"expectation_version"}))
        result = evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected)
    assert codes(result) == (
        "addressed_question_not_allowed", "structure_not_allowed", "next_focus_not_allowed",
        "strengths_must_be_empty", "missing_information_must_be_empty",
    )


def test_unconstrained_evaluation_has_no_universal_enum_or_criticism_policy():
    supplied = context(answer="")
    expected = SemanticDiagnosisExpectation()
    for addressed, structure, focus in itertools.product(
        get_args(AddressedQuestion), get_args(StructureAssessment), get_args(NextFocus),
    ):
        candidate = diagnosis(
            addressed_question=addressed, structure=structure, next_focus=focus,
            strengths=(), missing_information=(),
        )
        assert evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected) == ()


def test_explicitly_matching_no_and_maintain_strengths_with_missing_items_is_accepted():
    supplied = context(answer="")
    candidate = diagnosis(
        addressed_question="no", next_focus="maintain_strengths",
        missing_information=("Synthetic missing item.",),
    )
    matching = SemanticDiagnosisExpectation(
        allowed_addressed_question=frozenset({"no"}),
        allowed_next_focus=frozenset({"maintain_strengths"}),
        missing_information_requirement="nonempty",
    )
    for expected in (SemanticDiagnosisExpectation(), matching):
        assert evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected) == ()


@pytest.mark.parametrize("count", (1, 2, 3))
def test_nonempty_constraints_do_not_assess_item_text_order_duplicates_or_count(count):
    candidate = diagnosis(
        strengths=tuple("Exact synthetic text.\nInternal whitespace." for _ in range(count)),
        missing_information=tuple("Other synthetic text: 中文 😀." for _ in range(count)),
        addressed_question_reason="Different valid wording.",
        structure_feedback="Different valid structure wording.",
        next_focus_reason="Different valid focus wording.",
        retry_instruction="Different valid instruction wording.",
    )
    expected = SemanticDiagnosisExpectation(strengths_requirement="nonempty", missing_information_requirement="nonempty")
    assert evaluate_semantic_diagnosis(context=context(), diagnosis=candidate, expectation=expected) == ()


def rich_context():
    """Supply nested objective facts without calculating or consulting them."""
    measurement = MeasurementSnapshot(
        measurement_version="speaking-metrics-v1", measurement_source="original_transcription",
        recognized_word_count=9, um_count=1, uh_count=0, filler_unavailable_reason=None,
        timed_utterance_span_seconds=6.5, estimated_words_per_minute=83.0769230769,
        timing_unavailable_reason=None,
    )
    change = MetricChange(
        before=None, after=None, delta=None, before_unavailable_reason="no_measurement",
        after_unavailable_reason="no_measurement", comparable=False,
        comparison_unavailable_reason="both_unavailable",
    )
    delivery_change = DeliveryMetricChange(**change.model_dump())
    previous = PreviousAttemptFacts(
        before_attempt_number=1, after_attempt_number=2,
        speaking=ComparisonMetrics(**{field: change for field in ComparisonMetrics.model_fields}),
        delivery=DeliveryComparison(
            before_version=None, after_version=None, before_source=None, after_source=None,
            pause_count=delivery_change, total_pause_duration_seconds=delivery_change,
            longest_pause_seconds=delivery_change,
        ),
    )
    return context(attempt_number=2, measurement=measurement, previous_attempt=previous)


def test_evaluation_preserves_input_values_nested_identities_and_reuses_supplied_models(monkeypatch):
    supplied = rich_context()
    candidate = diagnosis()
    expected = SemanticDiagnosisExpectation(allowed_addressed_question=frozenset({"yes"}))
    models = (supplied, candidate, expected)
    before = tuple(item.model_dump() for item in models)
    nested = (supplied.measurement, supplied.previous_attempt, candidate.strengths, expected.allowed_addressed_question)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Evaluation must not serialize, revalidate, copy, or reconstruct inputs.")

    with monkeypatch.context() as guarded:
        for model in (DiagnosisContext, SemanticDiagnosis, SemanticDiagnosisExpectation):
            for name in MODEL_METHODS:
                guarded.setattr(model, name, forbidden)
        first = evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected)
        second = evaluate_semantic_diagnosis(context=supplied, diagnosis=candidate, expectation=expected)
    assert calls == []
    assert codes(first) == codes(second) == ("addressed_question_not_allowed",)
    assert first == second
    assert models[0] is supplied and models[1] is candidate and models[2] is expected
    assert tuple(item.model_dump() for item in models) == before
    after_nested = (supplied.measurement, supplied.previous_attempt, candidate.strengths, expected.allowed_addressed_question)
    assert all(after is original for after, original in zip(after_nested, nested))


def test_runtime_imports_are_limited_to_typing_pydantic_and_existing_contracts():
    tree = ast.parse(Path(evaluator.__file__).read_text())
    allowed = {"typing", "pydantic", "app.diagnosis", "app.semantic_diagnosis"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(item.name in allowed for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed
            assert all(item.name != "*" for item in node.names)


def test_evaluation_does_not_access_external_boundaries(monkeypatch, capsys, caplog):
    import builtins
    from collections.abc import Mapping
    import datetime
    import io
    import logging
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    import uuid

    # Construct all fixtures and warm violation validation before guarding I/O.
    supplied = rich_context()
    passing = diagnosis()
    constrained = SemanticDiagnosisExpectation(
        allowed_addressed_question=frozenset({"yes"}), allowed_structure=frozenset({"clear"}),
        allowed_next_focus=frozenset({"maintain_strengths"}), strengths_requirement="empty",
        missing_information_requirement="empty",
    )
    unconstrained = SemanticDiagnosisExpectation()
    SemanticEvalViolation(code=VIOLATION_CODES[0])
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Semantic evaluation must use only supplied constraints and diagnosis fields.")

    class ForbiddenDateTime(datetime.datetime):
        now = classmethod(forbidden)
        utcnow = classmethod(forbidden)
        today = classmethod(forbidden)

    class ForbiddenDate(datetime.date):
        today = classmethod(forbidden)

    class ForbiddenEnvironment(Mapping):
        __getitem__ = forbidden
        __iter__ = forbidden
        __len__ = forbidden
        get = forbidden
        copy = forbidden

    with monkeypatch.context() as guarded:
        for owner, names in (
            (builtins, ("open", "print")),
            (io, ("open",)),
            (os, ("open", "listdir", "scandir", "stat", "getenv", "putenv", "unsetenv", "urandom")),
            (Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes", "iterdir")),
            (socket, ("socket", "create_connection", "getaddrinfo")),
            (urllib.request, ("urlopen",)),
            (subprocess, ("Popen", "run", "call", "check_call", "check_output")),
            (uuid, ("UUID", "uuid1", "uuid3", "uuid4", "uuid5")),
            (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time")),
            (logging.Logger, ("_log",)),
        ):
            for name in names:
                guarded.setattr(owner, name, forbidden)
        guarded.setattr(os, "environ", ForbiddenEnvironment())
        guarded.setattr(datetime, "datetime", ForbiddenDateTime)
        guarded.setattr(datetime, "date", ForbiddenDate)

        # Patch already loaded boundaries; do not import providers or databases.
        for module_name, names in (
            ("sqlalchemy", ("create_engine",)),
            ("sqlalchemy.engine", ("create_engine",)),
            ("app.database", ("create_database_engine", "create_session_factory")),
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                for name in names:
                    if name in vars(module):
                        guarded.setattr(module, name, forbidden)
        for module_name, class_name, method_name in (
            ("sqlalchemy.engine", "Connection", "execute"),
            ("sqlalchemy.orm", "Session", "execute"),
            ("sqlalchemy.ext.asyncio", "AsyncSession", "execute"),
            ("httpx", "Client", "request"),
            ("httpx", "AsyncClient", "request"),
            ("httpx2", "Client", "request"),
            ("httpx2", "AsyncClient", "request"),
            ("requests", "Session", "request"),
            ("openai", "OpenAI", "__init__"),
            ("openai", "AsyncOpenAI", "__init__"),
            ("anthropic", "Anthropic", "__init__"),
            ("anthropic", "AsyncAnthropic", "__init__"),
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                guarded.setattr(owner, method_name, forbidden)
        guarded.setattr(builtins, "__import__", forbidden)
        pass_result = evaluate_semantic_diagnosis(context=supplied, diagnosis=passing, expectation=unconstrained)
        fail_result = evaluate_semantic_diagnosis(context=supplied, diagnosis=passing, expectation=constrained)

    # Clock/environment/import guards are restored before pytest assertions.
    assert calls == []
    assert pass_result == ()
    assert codes(fail_result) == (
        "addressed_question_not_allowed", "structure_not_allowed", "next_focus_not_allowed",
        "strengths_must_be_empty", "missing_information_must_be_empty",
    )
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []

"""Offline contract tests for the semantic feedback output schema."""

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from app.semantic_diagnosis import (
    SEMANTIC_DIAGNOSIS_VERSION,
    AddressedQuestion,
    NextFocus,
    SemanticDiagnosis,
    StructureAssessment,
)


FIELDS = (
    "diagnosis_version", "addressed_question", "addressed_question_reason",
    "strengths", "missing_information", "structure", "structure_feedback",
    "next_focus", "next_focus_reason", "retry_instruction",
)
TEXT_FIELDS = (
    "addressed_question_reason", "strengths", "missing_information",
    "structure_feedback", "next_focus_reason", "retry_instruction",
)
COLLECTION_FIELDS = ("strengths", "missing_information")
ENUMS = {
    "addressed_question": ("yes", "partially", "no"),
    "structure": ("clear", "mixed", "unclear", "insufficient_content"),
    "next_focus": (
        "answer_the_question", "specificity", "supporting_detail", "structure",
        "completeness", "conciseness", "maintain_strengths",
    ),
}
FORBIDDEN_FIELDS = (
    "id", "session_id", "attempt_id", "attempt_number", "question_index",
    "measurement_id", "measurement_version", "measurement_source",
    "recognized_word_count", "um_count", "uh_count",
    "timed_utterance_span_seconds", "estimated_words_per_minute",
    "pause_count", "total_pause_duration_seconds", "longest_pause_seconds",
    "score", "scores", "confidence", "objective", "objective_metrics",
    "question", "answer", "transcript", "language", "provider_payload",
    "metadata", "measurement", "previous_attempt", "comparison",
    "delivery_metrics", "before", "after", "delta", "comparable",
    "retry_question", "active_question", "current_question_index",
    "advance_to_next_question", "next_question", "follow_up_question",
    "action", "state", "session_state", "should_advance", "should_retry",
    "completed",
)
UNICODE_WHITESPACE = (
    " ", "\t", "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x1f",
    "\u0085", "\u00a0", "\u1680", "\u2000", "\u2001", "\u2002", "\u2003",
    "\u2004", "\u2005", "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",
    "\u2028", "\u2029", "\u202f", "\u205f", "\u3000",
)


def payload(**updates):
    values = {
        "addressed_question": "partially",
        "addressed_question_reason": "The decision is described, but its outcome is missing.",
        "strengths": (
            "Your opening identifies the decision.",
            "You name the criteria you used.",
        ),
        "missing_information": ("Explain the result of the decision.",),
        "structure": "mixed",
        "structure_feedback": "The decision and criteria are in a clear sequence.",
        "next_focus": "supporting_detail",
        "next_focus_reason": "An outcome would make the example more complete.",
        "retry_instruction": "Describe the decision, your criteria, and what happened next.",
    }
    values.update(updates)
    return values


def with_text(field, value):
    return payload(**{field: (value,) if field in COLLECTION_FIELDS else value})


def json_payload(values):
    return json.dumps(values, ensure_ascii=False)


def schema_property_names(value):
    """Collect property names at every depth, including any future definitions."""
    if isinstance(value, dict):
        yield from value.get("properties", {})
        for child in value.values():
            yield from schema_property_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from schema_property_names(child)


def test_default_version_and_public_literal_aliases_are_exact():
    assert SEMANTIC_DIAGNOSIS_VERSION == "semantic-diagnosis-v1"
    assert get_args(AddressedQuestion) == ENUMS["addressed_question"]
    assert get_args(StructureAssessment) == ENUMS["structure"]
    assert get_args(NextFocus) == ENUMS["next_focus"]
    defaulted = SemanticDiagnosis.model_validate(payload())
    explicit = SemanticDiagnosis.model_validate(payload(diagnosis_version=SEMANTIC_DIAGNOSIS_VERSION))
    assert defaulted == explicit
    assert defaulted.diagnosis_version == SEMANTIC_DIAGNOSIS_VERSION


@pytest.mark.parametrize(("field", "value"), [
    (field, value) for field, values in ENUMS.items() for value in values
])
def test_each_allowed_enum_value_is_preserved_without_semantic_inference(field, value):
    data = payload(**{field: value})
    result = SemanticDiagnosis.model_validate(data)
    assert getattr(result, field) == value
    assert result.model_dump(exclude={"diagnosis_version"}) == data
    assert SemanticDiagnosis.model_validate_json(json_payload(data)) == result


@pytest.mark.parametrize("field", ENUMS)
@pytest.mark.parametrize("value", ["unknown", "YES", "yes ", "", None, True, 1, b"yes", {}])
def test_unknown_or_non_string_enum_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(payload(**{field: value}))


@pytest.mark.parametrize("value", [
    "semantic-diagnosis-v2", "semantic-diagnosis-v1 ", "", None, True,
    1, 1.0, b"semantic-diagnosis-v1", {}, [],
])
def test_wrong_version_is_rejected(value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(payload(diagnosis_version=value))


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", [None, True, 1, 1.5, b"feedback", bytearray(b"feedback"), [], {}, ("feedback",)])
def test_every_feedback_text_position_requires_a_strict_string(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(with_text(field, value))


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", [None, True, 1, 1.5, [], {}])
def test_json_feedback_text_positions_also_reject_scalar_coercion(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(with_text(field, value)))


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_empty_blank_and_padded_unicode_feedback_are_rejected_without_stripping(field):
    for value in ("", *(text for whitespace in UNICODE_WHITESPACE for text in (
        whitespace, whitespace + "Feedback", "Feedback" + whitespace,
    ))):
        with pytest.raises(ValidationError):
            SemanticDiagnosis.model_validate(with_text(field, value))
        with pytest.raises(ValidationError):
            SemanticDiagnosis.model_validate_json(json_payload(with_text(field, value)))


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["x" * 600, "😀" * 600, "e\u0301" * 300])
def test_six_hundred_unicode_codepoints_are_accepted_without_byte_or_grapheme_limits(field, value):
    assert len(value) == 600
    result = SemanticDiagnosis.model_validate(with_text(field, value))
    assert getattr(result, field) == ((value,) if field in COLLECTION_FIELDS else value)
    assert SemanticDiagnosis.model_validate_json(json_payload(with_text(field, value))) == result


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["x" * 601, "😀" * 601, "e\u0301" * 300 + "e"])
def test_six_hundred_and_one_unicode_codepoints_are_rejected_without_truncation(field, value):
    assert len(value) == 601
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(with_text(field, value))
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(with_text(field, value)))


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["x", "😀", "é", "e\u0301 / ﬁ / 中文 / 😀  first\tsecond\nthird"])
def test_feedback_preserves_unicode_normalization_and_internal_whitespace(field, value):
    result = SemanticDiagnosis.model_validate(with_text(field, value))
    expected = (value,) if field in COLLECTION_FIELDS else value
    assert getattr(result, field) == expected
    assert result.model_dump()[field] == expected
    assert json.loads(result.model_dump_json())[field] == (list(expected) if field in COLLECTION_FIELDS else expected)
    assert SemanticDiagnosis.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
@pytest.mark.parametrize("count", range(4))
def test_feedback_collections_allow_zero_to_three_items_and_json_arrays_become_tuples(field, count):
    items = tuple(f"Feedback item {index}." for index in range(count))
    python_result = SemanticDiagnosis.model_validate(payload(**{field: items}))
    json_result = SemanticDiagnosis.model_validate_json(json_payload(payload(**{field: list(items)})))
    assert python_result == json_result
    assert type(getattr(json_result, field)) is tuple
    assert getattr(json_result, field) == items
    assert json_result.model_dump(mode="json")[field] == list(items)


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
def test_four_feedback_items_are_rejected_in_python_and_json(field):
    items = ("One.", "Two.", "Three.", "Four.")
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(payload(**{field: items}))
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(payload(**{field: list(items)})))


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
@pytest.mark.parametrize("value", [None, "Feedback", b"Feedback", [], ["Feedback"], {"Feedback"}, frozenset({"Feedback"}), {"item": "Feedback"}])
def test_python_strict_collection_fields_reject_non_tuple_inputs(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(payload(**{field: value}))


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
def test_python_strict_collection_fields_reject_iterators(field):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate(payload(**{field: iter(("Feedback",))}))


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
@pytest.mark.parametrize("value", [None, "Feedback", True, 1, {"item": "Feedback"}])
def test_json_collection_fields_require_arrays(field, value):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(payload(**{field: value})))


def test_collection_order_duplicates_and_exact_text_are_preserved():
    strengths = ("Second point.", "First point.", "Second point.")
    missing = ("Give an example.", "Give an example.", "Explain its result.")
    data = payload(strengths=strengths, missing_information=missing)
    before = dict(data)
    result = SemanticDiagnosis.model_validate(data)
    assert data == before
    assert result.strengths == strengths
    assert result.missing_information == missing
    encoded = json.loads(result.model_dump_json())
    assert encoded["strengths"] == list(strengths)
    assert encoded["missing_information"] == list(missing)
    assert SemanticDiagnosis.model_validate_json(json_payload(data)) == result


@pytest.mark.parametrize("field", FIELDS)
def test_every_model_field_is_frozen_for_assignment_and_deletion(field):
    result = SemanticDiagnosis.model_validate(payload())
    before = result.model_dump()
    with pytest.raises(ValidationError) as assignment:
        setattr(result, field, getattr(result, field))
    assert assignment.value.errors()[0]["type"] == "frozen_instance"
    with pytest.raises(ValidationError) as deletion:
        delattr(result, field)
    assert deletion.value.errors()[0]["type"] == "frozen_instance"
    assert result.model_dump() == before


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
def test_feedback_items_are_immutable_tuples_after_json_validation(field):
    result = SemanticDiagnosis.model_validate_json(json_payload(payload()))
    items = getattr(result, field)
    before = result.model_dump()
    assert type(items) is tuple
    with pytest.raises(TypeError):
        items[0] = "Changed feedback."
    with pytest.raises(ValidationError):
        setattr(result, field, items + ("Changed feedback.",))
    assert result.model_dump() == before


@pytest.mark.parametrize("field", FIELDS[1:])
def test_all_fields_except_the_defaulted_version_are_required(field):
    data = payload()
    del data[field]
    with pytest.raises(ValidationError) as error:
        SemanticDiagnosis.model_validate(data)
    assert any(item["loc"] == (field,) and item["type"] == "missing" for item in error.value.errors())
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(data))


@pytest.mark.parametrize("field", (*FORBIDDEN_FIELDS, "unexpected_field"))
def test_extra_identity_objective_state_and_arbitrary_fields_are_forbidden(field):
    data = payload(**{field: {"private": "content"}})
    with pytest.raises(ValidationError) as error:
        SemanticDiagnosis.model_validate(data)
    assert any(item["loc"] == (field,) and item["type"] == "extra_forbidden" for item in error.value.errors())
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(json_payload(data))


def test_json_schema_has_only_the_exact_feedback_fields_and_bounded_values():
    schema = SemanticDiagnosis.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert tuple(SemanticDiagnosis.model_fields) == FIELDS
    assert tuple(schema["properties"]) == FIELDS
    assert tuple(schema["required"]) == FIELDS[1:]
    assert schema["properties"]["diagnosis_version"]["const"] == SEMANTIC_DIAGNOSIS_VERSION
    assert schema["properties"]["diagnosis_version"]["default"] == SEMANTIC_DIAGNOSIS_VERSION
    for field, values in ENUMS.items():
        assert schema["properties"][field]["type"] == "string"
        assert tuple(schema["properties"][field]["enum"]) == values
    for field in TEXT_FIELDS:
        field_schema = schema["properties"][field]
        if field in COLLECTION_FIELDS:
            assert field_schema["type"] == "array"
            assert field_schema.get("minItems", 0) == 0
            assert field_schema["maxItems"] == 3
            field_schema = field_schema["items"]
        assert field_schema["type"] == "string"
        assert field_schema["minLength"] == 1
        assert field_schema["maxLength"] == 600


def test_schema_and_serialized_output_exclude_objective_facts_and_workflow_state_recursively():
    schema_fields = set(schema_property_names(SemanticDiagnosis.model_json_schema()))
    assert schema_fields == set(FIELDS)
    assert schema_fields.isdisjoint(FORBIDDEN_FIELDS)
    result = SemanticDiagnosis.model_validate(payload())
    assert set(result.model_dump()) == set(FIELDS)
    assert set(json.loads(result.model_dump_json())) == set(FIELDS)
    assert all(type(value) in (str, tuple) for value in result.model_dump().values())


def test_python_and_json_round_trips_preserve_field_order_and_immutable_collections():
    result = SemanticDiagnosis.model_validate(payload())
    python_dump = result.model_dump()
    json_dump = result.model_dump(mode="json")
    decoded = json.loads(result.model_dump_json())
    assert tuple(python_dump) == tuple(json_dump) == tuple(decoded) == FIELDS
    assert type(python_dump["strengths"]) is type(python_dump["missing_information"]) is tuple
    assert type(decoded["strengths"]) is type(decoded["missing_information"]) is list
    assert decoded == json_dump
    assert SemanticDiagnosis.model_validate(python_dump) == result
    assert SemanticDiagnosis.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("document", ["", "{", '{"addressed_question":', "null", "[]", '"feedback"', b"\xff"])
def test_malformed_json_or_a_non_object_document_is_rejected(document):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(document)


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_feedback_keywords_do_not_trigger_content_censorship(field):
    text = (
        "Discuss score, confidence, session_id, transcript, database, and objective metrics; "
        "the example says next_question and retry_instruction, plus "
        "MOVE_ON, RETRY, FOLLOW_UP, CLARIFY, and CHALLENGE."
    )
    result = SemanticDiagnosis.model_validate(with_text(field, text))
    assert getattr(result, field) == ((text,) if field in COLLECTION_FIELDS else text)
    assert SemanticDiagnosis.model_validate_json(result.model_dump_json()) == result


def test_validation_and_serialization_do_not_access_io_logs_ids_clocks_or_environment(
    monkeypatch, capsys, caplog,
):
    import builtins
    from collections.abc import Mapping
    import datetime
    import io
    import logging
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    import uuid

    from app import semantic_diagnosis

    data = payload()
    encoded = json_payload(data)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Semantic diagnosis validation must use only supplied data.")

    real_datetime = datetime.datetime
    real_date = datetime.date
    real_environ = os.environ
    real_getenv = os.getenv
    clock_functions = (time.time, time.monotonic, time.perf_counter)

    class ForbiddenDateTime(real_datetime):
        now = classmethod(forbidden)
        utcnow = classmethod(forbidden)
        today = classmethod(forbidden)

    class ForbiddenDate(real_date):
        today = classmethod(forbidden)

    class ForbiddenEnvironment(Mapping):
        __getitem__ = forbidden
        __iter__ = forbidden
        __len__ = forbidden
        get = forbidden
        copy = forbidden

    guarded_environment = ForbiddenEnvironment()

    with monkeypatch.context() as guarded:
        for owner, name in (
            (builtins, "open"), (builtins, "print"), (io, "open"), (os, "open"),
            (Path, "open"), (socket, "socket"), (socket, "create_connection"),
            (socket, "getaddrinfo"), (urllib.request, "urlopen"),
            (subprocess, "Popen"), (subprocess, "run"),
            (uuid, "uuid1"), (uuid, "uuid4"), (os, "urandom"),
            (logging.Logger, "_log"),
        ):
            guarded.setattr(owner, name, forbidden)

        # Conftest has already loaded database modules. Guard those boundaries
        # without importing database or provider code for this output contract.
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
            ("httpx", "Client", "request"),
            ("httpx", "AsyncClient", "request"),
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                guarded.setattr(owner, method_name, forbidden)

        # Guard imported aliases as well as canonical clocks and environment.
        for name, value in tuple(vars(semantic_diagnosis).items()):
            if value is real_datetime:
                guarded.setattr(semantic_diagnosis, name, ForbiddenDateTime)
            elif value is real_date:
                guarded.setattr(semantic_diagnosis, name, ForbiddenDate)
            elif value is real_environ:
                guarded.setattr(semantic_diagnosis, name, guarded_environment)
            elif value is real_getenv:
                guarded.setattr(semantic_diagnosis, name, forbidden)
            elif any(value is clock for clock in clock_functions):
                guarded.setattr(semantic_diagnosis, name, forbidden)
        guarded.setattr(os, "getenv", forbidden)
        guarded.setattr(os, "environ", guarded_environment)
        guarded.setattr(datetime, "datetime", ForbiddenDateTime)
        guarded.setattr(datetime, "date", ForbiddenDate)
        for name in ("time", "monotonic", "perf_counter"):
            guarded.setattr(time, name, forbidden)

        # Restore environment and clock guards before framework assertions.
        from_python = SemanticDiagnosis.model_validate(data)
        from_json = SemanticDiagnosis.model_validate_json(encoded)
        python_dump = from_python.model_dump()
        json_dump = from_json.model_dump_json()

    assert calls == []
    assert from_python == from_json
    assert python_dump["strengths"] == data["strengths"]
    assert json.loads(json_dump) == from_python.model_dump(mode="json")
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert caplog.records == []

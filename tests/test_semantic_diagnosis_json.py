"""Offline JSON decoding contracts, using the existing semantic model's rules.

All payloads are synthetic. Model metadata supplies enum values, required
fields, and bounds so this boundary does not acquire a second semantic schema.
"""

import ast
import inspect
import json
from pathlib import Path
import traceback
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from app.semantic_diagnosis import SemanticDiagnosis
from app import semantic_diagnosis_json as boundary
from app.semantic_diagnosis_json import (
    SemanticDiagnosisJSONContractError, parse_semantic_diagnosis_json,
)


TYPE_ERROR = "Semantic diagnosis JSON payload must be a string."
CONTRACT_ERROR = "Semantic diagnosis output did not match the required contract."
SECRET = "SYNTHETIC_PRIVATE_PROVIDER_OUTPUT_7A9C"
TEXT = f"{SECRET}: café / e\u0301 / ﬁ / 中文 / 😀 first\tsecond\nthird"
PROPERTIES = SemanticDiagnosis.model_json_schema()["properties"]
REQUIRED_FIELDS = tuple(name for name, field in SemanticDiagnosis.model_fields.items() if field.is_required())
ENUM_FIELDS = tuple(name for name, prop in PROPERTIES.items() if "enum" in prop)
CONST_FIELDS = tuple(name for name, prop in PROPERTIES.items() if "const" in prop)
SCALAR_TEXT_FIELDS = tuple(name for name, prop in PROPERTIES.items() if prop.get("type") == "string" and "maxLength" in prop)
COLLECTION_FIELDS = tuple(name for name, prop in PROPERTIES.items() if prop.get("type") == "array")
TEXT_POSITIONS = (*SCALAR_TEXT_FIELDS, *COLLECTION_FIELDS)


def diagnosis():
    """Build valid arbitrary content using fields and alternatives from the model."""
    values = {}
    for name in REQUIRED_FIELDS:
        prop = PROPERTIES[name]
        if "enum" in prop:
            values[name] = get_args(SemanticDiagnosis.model_fields[name].annotation)[0]
        elif prop["type"] == "array":
            values[name] = (TEXT,)
        else:
            values[name] = TEXT
    return SemanticDiagnosis(**values)


def encoded(values):
    return json.dumps(values, ensure_ascii=False)


def with_text(values, field, value):
    result = dict(values)
    result[field] = [value] if field in COLLECTION_FIELDS else value
    return result


def assert_contract_rejected(payload, capsys, caplog):
    with pytest.raises(SemanticDiagnosisJSONContractError) as caught:
        parse_semantic_diagnosis_json(payload)
    error = caught.value
    assert type(error) is SemanticDiagnosisJSONContractError
    assert error.args == (CONTRACT_ERROR,)
    assert str(error) == CONTRACT_ERROR
    assert repr(error) == f"SemanticDiagnosisJSONContractError({CONTRACT_ERROR!r})"
    if payload.strip():
        assert payload not in str(error)
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    public_traceback = "".join(traceback.format_exception(error))
    assert SECRET not in public_traceback
    assert "ValidationError" not in public_traceback
    assert "validation error" not in public_traceback.lower()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []
    return error


def contract_failure_cases():
    baseline = diagnosis().model_dump(mode="json")
    cases = []

    def add(label, values):
        cases.append(pytest.param(encoded(values), id=label))

    for field in REQUIRED_FIELDS:
        values = dict(baseline)
        del values[field]
        add(f"missing-{field}", values)
    add("extra-top-level-field", {**baseline, "unexpected_synthetic_field": SECRET})
    for field in CONST_FIELDS:
        add(f"wrong-constant-{field}", {**baseline, field: f"{PROPERTIES[field]['const']}-incorrect-{SECRET}"})
    for field in ENUM_FIELDS:
        add(f"unknown-enum-{field}", {**baseline, field: f"unknown-{SECRET}"})
    for field, prop in PROPERTIES.items():
        if prop.get("type") == "string":
            for label, value in (
                ("null", None), ("bool", True), ("int", 17), ("float", 1.25),
                ("array", [SECRET]), ("object", {"synthetic_secret": SECRET}),
            ):
                add(f"wrong-scalar-{field}-{label}", {**baseline, field: value})
    for field in TEXT_POSITIONS:
        for label, value in (
            ("empty", ""), ("blank", " \t\n"), ("leading-space", " " + TEXT),
            ("trailing-space", TEXT + " "), ("leading-unicode-space", "\u2003" + TEXT),
            ("trailing-unicode-space", TEXT + "\u00a0"),
        ):
            add(f"feedback-{field}-{label}", with_text(baseline, field, value))
        prop = PROPERTIES[field]["items"] if field in COLLECTION_FIELDS else PROPERTIES[field]
        overlong = SECRET + "x" * (prop["maxLength"] + 1 - len(SECRET))
        add(f"feedback-{field}-overlong", with_text(baseline, field, overlong))
    for field in COLLECTION_FIELDS:
        add(f"collection-{field}-too-many", {**baseline, field: [TEXT] * (PROPERTIES[field]["maxItems"] + 1)})
        for label, value in (
            ("null", None), ("string", TEXT), ("bool", True), ("int", 17),
            ("object", {"synthetic_secret": SECRET}),
        ):
            add(f"collection-{field}-wrong-container-{label}", {**baseline, field: value})
        for label, value in (
            ("null", None), ("bool", True), ("int", 17), ("float", 1.25),
            ("array", [SECRET]), ("object", {"synthetic_secret": SECRET}),
        ):
            add(f"collection-{field}-wrong-member-{label}", {**baseline, field: [value]})
    return cases


def malformed_cases():
    valid = diagnosis().model_dump_json()
    return (
        pytest.param("", id="empty"),
        pytest.param(" \t\r\n", id="whitespace-only"),
        pytest.param(valid[:-1], id="truncated-object"),
        pytest.param(valid + " trailing " + SECRET, id="trailing-garbage"),
        pytest.param("{'synthetic_secret': '" + SECRET + "'}", id="single-quotes"),
        pytest.param("```json\n" + valid + "\n```", id="code-fence"),
        pytest.param("Synthetic prose " + SECRET + "\n" + valid, id="prose-before"),
        pytest.param(valid + "\nSynthetic prose " + SECRET, id="prose-after"),
        pytest.param(valid + valid, id="concatenated-objects"),
        pytest.param("[" + valid + "]", id="json-array"),
        pytest.param(encoded(SECRET), id="json-string"),
        pytest.param("17", id="json-number"),
        pytest.param("null", id="json-null"),
        pytest.param("true", id="json-bool"),
    )


def adversarial_cases():
    baseline = diagnosis().model_dump(mode="json")
    valid = encoded(baseline)
    selected_enum = ENUM_FIELDS[0]
    enum_value = get_args(SemanticDiagnosis.model_fields[selected_enum].annotation)[0]
    renamed = dict(baseline)
    renamed[SCALAR_TEXT_FIELDS[0] + "_alias"] = renamed.pop(SCALAR_TEXT_FIELDS[0])
    return (
        pytest.param("```json\n" + valid + "\n```", id="do-not-strip-json-fences"),
        pytest.param("```\n" + valid + "\n```", id="do-not-strip-plain-fences"),
        pytest.param("PREFIX " + SECRET + " " + valid + " SUFFIX", id="do-not-extract-object"),
        pytest.param(repr(baseline), id="do-not-replace-single-quotes"),
        pytest.param(valid[:-1] + ",}", id="do-not-remove-trailing-comma"),
        pytest.param(encoded(renamed), id="do-not-rename-fields"),
        pytest.param(encoded({**baseline, selected_enum: enum_value.upper()}), id="do-not-lowercase-enum"),
        pytest.param(encoded(with_text(baseline, SCALAR_TEXT_FIELDS[0], " " + TEXT + " ")), id="do-not-trim-feedback"),
        pytest.param(encoded({**baseline, "discard_me": SECRET}), id="do-not-drop-extra-field"),
    )


def test_public_parser_signature_and_exception_are_exact():
    assert issubclass(SemanticDiagnosisJSONContractError, RuntimeError)
    assert not inspect.iscoroutinefunction(parse_semantic_diagnosis_json)
    signature = inspect.signature(parse_semantic_diagnosis_json)
    assert tuple(signature.parameters) == ("payload",)
    parameter = signature.parameters["payload"]
    assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(parse_semantic_diagnosis_json) == {"payload": str, "return": SemanticDiagnosis}


@pytest.mark.parametrize("surrounding", ("", " ", "\t\r\n", " \n\t\r "))
def test_valid_json_preserves_every_field_unicode_internal_whitespace_and_input(surrounding):
    expected = diagnosis()
    payload = surrounding + expected.model_dump_json() + surrounding
    original = payload[:]
    original_bytes = payload.encode("utf-8")
    first = parse_semantic_diagnosis_json(payload)
    second = parse_semantic_diagnosis_json(payload=payload)

    assert type(first) is type(second) is SemanticDiagnosis
    assert first == second == expected
    assert first.model_dump() == expected.model_dump()
    assert payload == original
    assert payload.encode("utf-8") == original_bytes
    for field in SCALAR_TEXT_FIELDS:
        assert getattr(first, field) == TEXT
    for field in COLLECTION_FIELDS:
        assert type(getattr(first, field)) is tuple
        assert getattr(first, field) == (TEXT,)


def test_only_existing_model_defaults_are_filled():
    expected = diagnosis()
    payload = encoded({name: value for name, value in expected.model_dump(mode="json").items() if name in REQUIRED_FIELDS})
    assert parse_semantic_diagnosis_json(payload) == expected


@pytest.mark.parametrize(("field", "value"), [
    (field, value) for field in ENUM_FIELDS
    for value in get_args(SemanticDiagnosis.model_fields[field].annotation)
])
def test_existing_model_enum_alternatives_are_accepted_without_semantic_policy(field, value):
    baseline = diagnosis().model_dump(mode="json")
    payload = encoded({**baseline, field: value})
    assert parse_semantic_diagnosis_json(payload) == SemanticDiagnosis.model_validate_json(payload)


@pytest.mark.parametrize("field", TEXT_POSITIONS)
def test_existing_maximum_text_length_is_accepted_without_a_parser_threshold(field):
    baseline = diagnosis().model_dump(mode="json")
    prop = PROPERTIES[field]["items"] if field in COLLECTION_FIELDS else PROPERTIES[field]
    maximum_text = "😀" * prop["maxLength"]
    payload = encoded(with_text(baseline, field, maximum_text))
    parsed = parse_semantic_diagnosis_json(payload)
    assert parsed == SemanticDiagnosis.model_validate_json(payload)
    assert getattr(parsed, field) == ((maximum_text,) if field in COLLECTION_FIELDS else maximum_text)


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
def test_existing_collection_bounds_order_and_duplicates_are_preserved(field):
    baseline = diagnosis().model_dump(mode="json")
    prop = PROPERTIES[field]
    for count in range(prop.get("minItems", 0), prop["maxItems"] + 1):
        items = [TEXT if index % 2 == 0 else "Synthetic second item." for index in range(count)]
        payload = encoded({**baseline, field: items})
        result = parse_semantic_diagnosis_json(payload)
        assert getattr(result, field) == tuple(items)
        assert result == SemanticDiagnosis.model_validate_json(payload)


def test_parser_delegates_exact_raw_string_once_and_returns_exact_validated_model(monkeypatch):
    expected = diagnosis()
    payload = " \t\n" + expected.model_dump_json() + "\r\n "
    calls = []

    def authoritative_validator(cls, supplied):
        calls.append((cls, supplied))
        return expected

    def forbidden(*args, **kwargs):
        raise AssertionError("The parser must use only the existing JSON validation boundary.")

    with monkeypatch.context() as guarded:
        guarded.setattr(SemanticDiagnosis, "model_validate_json", classmethod(authoritative_validator))
        for name in (
            "__init__", "model_validate", "model_validate_strings", "model_construct",
            "model_copy", "model_dump", "model_dump_json", "model_json_schema",
        ):
            guarded.setattr(SemanticDiagnosis, name, forbidden)
        guarded.setattr(json, "loads", forbidden)
        guarded.setattr(json, "dumps", forbidden)
        result = parse_semantic_diagnosis_json(payload)

    assert result is expected
    assert len(calls) == 1
    assert calls[0][0] is SemanticDiagnosis
    assert calls[0][1] is payload


@pytest.mark.parametrize("kind", (
    "none", "dict", "list", "tuple", "bytes", "bytearray", "memoryview",
    "int", "float", "bool", "object", "diagnosis",
))
def test_non_strings_are_rejected_with_fixed_type_error_before_validation(monkeypatch, kind):
    valid_model = diagnosis()
    valid = valid_model.model_dump_json()
    invalid = {
        "none": None, "dict": valid_model.model_dump(), "list": [valid], "tuple": (valid,),
        "bytes": valid.encode(), "bytearray": bytearray(valid.encode()),
        "memoryview": memoryview(valid.encode()), "int": 17, "float": 1.25,
        "bool": True, "object": object(), "diagnosis": valid_model,
    }[kind]

    def forbidden(*args, **kwargs):
        raise AssertionError("Non-string inputs must not reach model validation.")

    monkeypatch.setattr(SemanticDiagnosis, "model_validate_json", forbidden)
    with pytest.raises(TypeError) as caught:
        parse_semantic_diagnosis_json(invalid)
    assert type(caught.value) is TypeError
    assert caught.value.args == (TYPE_ERROR,)
    assert str(caught.value) == TYPE_ERROR


def test_arbitrary_input_is_never_coerced_to_str_or_formatted():
    calls = []

    class MustNotCoerce:
        def __str__(self):
            calls.append("str")
            raise AssertionError("Input must not be coerced to a string.")

        def __repr__(self):
            calls.append("repr")
            raise AssertionError("Input must not be formatted in a public error.")

    with pytest.raises(TypeError) as caught:
        parse_semantic_diagnosis_json(MustNotCoerce())
    assert str(caught.value) == TYPE_ERROR
    assert calls == []


@pytest.mark.parametrize("payload", malformed_cases())
def test_malformed_or_non_object_json_has_only_private_fixed_contract_error(payload, capsys, caplog):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(payload)
    assert_contract_rejected(payload, capsys, caplog)


@pytest.mark.parametrize("payload", contract_failure_cases())
def test_existing_model_contract_failures_are_rejected_without_exposing_payload(payload, capsys, caplog):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(payload)
    assert_contract_rejected(payload, capsys, caplog)


@pytest.mark.parametrize("payload", adversarial_cases())
def test_adversarial_inputs_are_rejected_without_json_or_feedback_repair(payload, capsys, caplog):
    with pytest.raises(ValidationError):
        SemanticDiagnosis.model_validate_json(payload)
    assert_contract_rejected(payload, capsys, caplog)


def test_validation_failure_is_wrapped_once_without_cause_context_or_details(monkeypatch, capsys, caplog):
    payload = encoded({"synthetic_secret": SECRET})
    try:
        SemanticDiagnosis.model_validate_json(payload)
    except ValidationError as error:
        original_error = error
    calls = []

    def authoritative_validator(cls, supplied):
        calls.append(supplied)
        raise original_error

    monkeypatch.setattr(SemanticDiagnosis, "model_validate_json", classmethod(authoritative_validator))
    result = assert_contract_rejected(payload, capsys, caplog)
    assert result is not original_error
    assert len(calls) == 1 and calls[0] is payload


def test_runtime_has_only_model_validation_imports_and_no_duplicate_or_repair_policy():
    tree = ast.parse(Path(boundary.__file__).read_text())
    allowed = {
        "pydantic": {"ValidationError"},
        "app.semantic_diagnosis": {"SemanticDiagnosis"},
    }
    semantic_literals = set(SemanticDiagnosis.model_fields)
    semantic_literals.update(value for field in ENUM_FIELDS for value in get_args(SemanticDiagnosis.model_fields[field].annotation))
    semantic_literals.update(PROPERTIES[field]["const"] for field in CONST_FIELDS)
    schema_bounds = {
        value for prop in PROPERTIES.values()
        for key, value in prop.items() if key in {"maxLength", "maxItems"}
    }
    blocked_operations = {
        "loads", "dumps", "decode", "encode", "strip", "lstrip", "rstrip", "lower",
        "casefold", "replace", "split", "splitlines", "find", "index", "search", "match",
        "fullmatch", "sub", "model_validate", "model_construct", "model_copy",
    }
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Import)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed
            assert {name.name for name in node.names} <= allowed[node.module]
            assert all(name.asname is None for name in node.names)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                assert node.value not in semantic_literals
            elif type(node.value) is int:
                assert node.value not in schema_bounds
        if isinstance(node, ast.Attribute):
            assert node.attr not in blocked_operations
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name == "parse_semantic_diagnosis_json"
        if isinstance(node, ast.Name):
            assert not any(word in node.id.lower() for word in ("repair", "extract", "score", "confidence", "ranking"))


def test_parsing_uses_no_files_network_environment_clock_database_or_provider_boundaries(monkeypatch, capsys, caplog):
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

    # Build and warm all fixture/model/schema work before installing global guards.
    expected = diagnosis()
    payload = " \n\t" + expected.model_dump_json() + "\r\n "
    assert parse_semantic_diagnosis_json(payload) == expected
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("JSON parsing must use only the supplied text and SemanticDiagnosis validation.")

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
            (builtins, ("open", "print")), (io, ("open",)),
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
        for module_name, class_name, names in (
            ("sqlalchemy.engine", "Engine", ("connect", "begin")),
            ("sqlalchemy.engine", "Connection", ("execute", "begin")),
            ("sqlalchemy.orm", "Session", ("execute", "begin", "add", "flush", "commit")),
            ("sqlalchemy.ext.asyncio", "AsyncSession", ("execute", "begin")),
            ("httpx", "Client", ("request", "send")),
            ("httpx", "AsyncClient", ("request", "send")),
            ("requests", "Session", ("request",)),
            ("openai", "OpenAI", ("__init__",)),
            ("openai", "AsyncOpenAI", ("__init__",)),
            ("openai.resources.chat.completions.completions", "Completions", ("create",)),
            ("openai.resources.chat.completions.completions", "AsyncCompletions", ("create",)),
            ("anthropic", "Anthropic", ("__init__",)),
            ("anthropic", "AsyncAnthropic", ("__init__",)),
            ("anthropic.resources.messages.messages", "Messages", ("create",)),
            ("anthropic.resources.messages.messages", "AsyncMessages", ("create",)),
        ):
            module = sys.modules.get(module_name)
            owner = vars(module).get(class_name) if module is not None else None
            if owner is not None:
                for name in names:
                    guarded.setattr(owner, name, forbidden)
        guarded.setattr(builtins, "__import__", forbidden)
        result = parse_semantic_diagnosis_json(payload)

    # Restore global clock/import/environment guards before pytest assertions.
    assert calls == []
    assert type(result) is SemanticDiagnosis
    assert result == expected
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert caplog.records == []

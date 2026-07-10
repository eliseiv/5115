"""Unit tests for ADR-057 — Study & Learn quiz.generate tool contract (pure, no I/O).

Covers the load-bearing invariants of the strict quiz function-tool:
- the OpenAI-strict wire schema for quiz.generate carries ``strict: true`` and every keyword
  POSITION in it is a member of the ``_OPENAI_STRICT_ALLOWED_KEYWORDS`` allowlist — the STRUCTURAL
  guard against the provider ``400 Invalid schema`` blocker (ADR-057 §2). An allowlist (not a
  denylist) is what protects a future strict tool (ADR-058 ``image.generate`` with ``size: str |
  None`` → ``anyOf``): an unknown keyword is DROPPED, never forwarded;
- non-strict tools are serialized UNCHANGED (their length constraints survive);
- ``_clean_for_openai_strict`` is a pure, SCHEMA-AWARE function (no input mutation; keeps only
  allowlisted keyword positions; preserves property/$defs NAMES incl. ones named like keywords);
- the provider-NEUTRAL schema keeps the numeric constraints (server-side validation source);
- the dialog-mode gate: quiz.generate offered ONLY under study_learn, composing with the temporary
  (include_client_side=False) axis;
- ``QuizGenerateArgs`` server-side validation (the SOLE enforcer of the cross-field/range/length
  invariants that strict cannot express);
- ``QuizSchema`` response-boundary validator (``correctIndex < len(options)``).

All assertions run against tools.py / schemas.chat directly (no network, no DB, no LLM call).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.chat.tools import (
    _OPENAI_STRICT_ALLOWED_KEYWORDS,
    _clean_for_openai_strict,
    neutral_tool_definitions,
    openai_tool_definitions,
    validate_tool_args,
)
from app.schemas.chat import QuizSchema

_VALID_QUIZ_ARGS: dict[str, Any] = {
    "question": "What is 2 + 2?",
    "options": ["3", "4", "5"],
    "correctIndex": 1,
    "explanation": "2 + 2 = 4.",
}


def _schema_keywords(node: Any) -> set[str]:
    """Every KEYWORD-position key appearing anywhere in a JSON Schema (SCHEMA-AWARE).

    Mirrors ``_clean_for_openai_strict``'s traversal EXACTLY: under ``properties``/``$defs`` the map
    keys are arbitrary field/definition NAMES (data, not keywords) and are NOT collected — only the
    subschemas are descended; ``anyOf``/``items``/``additionalProperties`` carry nested subschemas
    that are descended; every other key is a leaf keyword (``type``/``required``/``$ref``/``enum``/
    ``const`` …) collected verbatim without recursing into its value. This is why a field literally
    named ``items`` or ``allOf`` never shows up as a keyword here — the whole point of the guard.
    """
    keys: set[str] = set()
    if not isinstance(node, dict):
        return keys
    for key, value in node.items():
        keys.add(key)
        if key in ("properties", "$defs") and isinstance(value, dict):
            for _name, subschema in value.items():
                keys |= _schema_keywords(subschema)
        elif key == "anyOf" and isinstance(value, list):
            for subschema in value:
                keys |= _schema_keywords(subschema)
        elif key == "items":
            if isinstance(value, list):
                for subschema in value:
                    keys |= _schema_keywords(subschema)
            else:
                keys |= _schema_keywords(value)
        elif key == "additionalProperties" and isinstance(value, dict):
            keys |= _schema_keywords(value)
        # else: leaf keyword — type/required/description/$ref + DATA in enum/const — no recurse
    return keys


def _openai_quiz_def() -> dict[str, Any]:
    defs = openai_tool_definitions(dialog_mode="study_learn")
    return next(d for d in defs if d["name"] == "quiz_generate")


# =================== static guard against the strict-400 blocker ===================
def test_openai_quiz_schema_keyword_positions_are_allowlist_subset() -> None:
    # ADR-057 §2 (the blocker guard, INVERTED to an allowlist): quiz.generate is serialized strict,
    # and EVERY keyword position in its wire schema must be a member of the allowlist. This is the
    # structural guarantee a denylist could not give — an unknown/unsupported keyword (title,
    # maxLength, allOf, patternProperties, …) is DROPPED, never forwarded to 400 the request. The
    # walk is schema-aware (field NAMES under properties/$defs are not keyword positions).
    quiz_def = _openai_quiz_def()
    assert quiz_def["strict"] is True
    present = _schema_keywords(quiz_def["parameters"])
    extra = present - set(_OPENAI_STRICT_ALLOWED_KEYWORDS)
    assert extra == set(), f"strict schema carries non-allowlisted keywords: {sorted(extra)}"
    # Spot-check the classic offenders that used to be denylisted are indeed absent.
    assert "title" not in present
    assert "maxLength" not in present
    assert "minItems" not in present


def test_openai_quiz_schema_keeps_strict_required_structure() -> None:
    # Structural sanity: strict-mode REQUIRES an object with every property in `required` and
    # additionalProperties:false (ADR-057 §2/§3). Stripping constraints must not damage this.
    params = _openai_quiz_def()["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"question", "options", "correctIndex", "explanation"}
    assert params["additionalProperties"] is False
    # The 4 properties survive the strip (types only, no constraints).
    assert set(params["properties"]) == {"question", "options", "correctIndex", "explanation"}
    assert params["properties"]["options"]["type"] == "array"
    assert params["properties"]["options"]["items"]["type"] == "string"


# ============================ non-strict tools untouched ============================
def test_non_strict_tool_keeps_constraints_and_is_not_strict() -> None:
    # Regression: a plain tool (files.read) is serialized strict=False with its schema UNCHANGED —
    # its length constraints (path.minLength) survive because no strict validator runs on it.
    defs = openai_tool_definitions(dialog_mode="study_learn")
    files_read = next(d for d in defs if d["name"] == "files_read")
    assert files_read["strict"] is False
    props = files_read["parameters"]["properties"]
    assert props["path"]["minLength"] == 1


# ============================ _clean_for_openai_strict purity ============================
def test_clean_for_openai_strict_does_not_mutate_input() -> None:
    # ADR-057 §2: the clean is a PURE function — the same neutral def can be serialized for other
    # providers afterwards. Feed it the real neutral quiz schema; assert the input is byte-identical
    # after the call (deep-copy compare) and the result is a distinct object.
    neutral = next(
        d
        for d in neutral_tool_definitions(dialog_mode="study_learn")
        if d["name"] == "quiz.generate"
    )
    original_schema = neutral["input_schema"]
    before = copy.deepcopy(original_schema)
    cleaned = _clean_for_openai_strict(original_schema)
    assert original_schema == before, "input schema was mutated"
    assert cleaned is not original_schema, "must return a NEW structure"
    # The cleaned copy actually reduced to the allowlist (proves it worked, not a no-op).
    assert _schema_keywords(cleaned) <= set(_OPENAI_STRICT_ALLOWED_KEYWORDS)
    assert "minItems" not in _schema_keywords(cleaned)
    assert "maxLength" not in _schema_keywords(cleaned)


def test_clean_for_openai_strict_allowlist_is_structurally_robust() -> None:
    # The point of the allowlist over a denylist (reviewer sprint-4 finding): a strict tool added in
    # a later sprint must not silently smuggle an unsupported keyword. Feed a synthetic schema that
    # packs the keyword shapes Pydantic v2 actually emits (anyOf+$defs+$ref for Optional[...], allOf
    # for a nested model) PLUS ones a denylist would miss (patternProperties, if/then, not,
    # dependentRequired) PLUS noise (title, default) PLUS a nested constraint (maxLength inside an
    # anyOf branch) PLUS the adversarial case: properties literally NAMED ``items`` and ``allOf``.
    schema = {
        "type": "object",
        "title": "Root",
        "default": {},
        "additionalProperties": False,
        "required": ["opt", "lit"],
        "patternProperties": {"^x": {"type": "string"}},
        "if": {"type": "object"},
        "then": {"type": "object"},
        "not": {"type": "null"},
        "dependentRequired": {"opt": ["lit"]},
        "allOf": [{"$ref": "#/$defs/Sub"}],
        "properties": {
            # Optional[str] shape: anyOf with a maxLength constraint buried in the first branch.
            "opt": {"anyOf": [{"type": "string", "maxLength": 5}, {"type": "null"}]},
            "lit": {"enum": ["a", "b"], "const": "a"},
            "ref": {"$ref": "#/$defs/Sub"},
            # Adversarial: a field whose NAME collides with a keyword must be preserved as a name.
            "items": {"type": "string", "maxLength": 3},
            "allOf": {"type": "integer", "minimum": 0},
        },
        "$defs": {
            "Sub": {
                "type": "object",
                "title": "Sub",
                "minimum": 0,
                "properties": {"k": {"type": "string", "maxLength": 9}},
            }
        },
    }
    cleaned = _clean_for_openai_strict(schema)

    # (1) EVERY keyword position is allowlisted — the structural guarantee.
    present = _schema_keywords(cleaned)
    assert present <= set(_OPENAI_STRICT_ALLOWED_KEYWORDS), sorted(
        present - set(_OPENAI_STRICT_ALLOWED_KEYWORDS)
    )

    # (2) Strict-supported keywords survive.
    assert cleaned["type"] == "object"
    assert cleaned["additionalProperties"] is False
    assert set(cleaned["required"]) == {"opt", "lit"}
    assert cleaned["properties"]["opt"]["anyOf"][0]["type"] == "string"
    assert cleaned["properties"]["lit"]["enum"] == ["a", "b"]
    assert cleaned["properties"]["lit"]["const"] == "a"
    assert cleaned["properties"]["ref"]["$ref"] == "#/$defs/Sub"
    assert "Sub" in cleaned["$defs"]
    assert cleaned["$defs"]["Sub"]["properties"]["k"]["type"] == "string"

    # (3) Field NAMES are never lost — even ones colliding with keyword names.
    assert set(cleaned["properties"]) == {"opt", "lit", "ref", "items", "allOf"}
    assert cleaned["properties"]["items"]["type"] == "string"
    assert cleaned["properties"]["allOf"]["type"] == "integer"

    # (4) Unknown/unsupported keywords AND nested constraints are dropped.
    for gone in (
        "title",
        "default",
        "patternProperties",
        "if",
        "then",
        "not",
        "dependentRequired",
        "allOf",  # top-level allOf keyword (NOT the property named "allOf") is dropped
        "maxLength",
        "minimum",
    ):
        assert gone not in present, gone
    # The maxLength buried in the anyOf branch is gone; the branch itself (a keyword) stayed.
    assert "maxLength" not in cleaned["properties"]["opt"]["anyOf"][0]
    # The $defs.Sub.title / .minimum are gone but the nested property NAME "k" is kept.
    assert "title" not in cleaned["$defs"]["Sub"]
    assert "minimum" not in cleaned["$defs"]["Sub"]
    assert "k" in cleaned["$defs"]["Sub"]["properties"]


# =============== neutral schema retains the server-side constraints ===============
def test_neutral_quiz_schema_keeps_numeric_constraints() -> None:
    # ADR-057 §2/§3: the constraints stay in the NEUTRAL schema (server-side validation source);
    # only the OpenAI wire copy is stripped. Anthropic and validate_tool_args rely on these.
    neutral = next(
        d
        for d in neutral_tool_definitions(dialog_mode="study_learn")
        if d["name"] == "quiz.generate"
    )
    schema = neutral["input_schema"]
    assert schema["properties"]["options"]["minItems"] == 2
    assert schema["properties"]["options"]["maxItems"] == 10
    assert schema["properties"]["question"]["maxLength"] == 1000
    assert schema["properties"]["options"]["items"]["maxLength"] == 400
    assert schema["properties"]["explanation"]["maxLength"] == 2000


# ============================ dialog-mode gate ============================
def _offered_names(dialog_mode: str | None, *, include_client_side: bool = True) -> set[str]:
    defs = neutral_tool_definitions(
        dialog_mode=dialog_mode, include_client_side=include_client_side
    )
    return {d["name"] for d in defs}


@pytest.mark.parametrize("dialog_mode", [None, "smart", "deep_thinking", "search"])
def test_quiz_generate_absent_outside_study_learn(dialog_mode: str | None) -> None:
    assert "quiz.generate" not in _offered_names(dialog_mode)


def test_quiz_generate_present_in_study_learn() -> None:
    assert "quiz.generate" in _offered_names("study_learn")


def test_quiz_gate_composes_with_temporary_chat() -> None:
    # ADR-056 + ADR-057: quiz.generate is a GLOBAL server-side tool, so include_client_side=False
    # (temporary chat) does NOT drop it; it stays offered under study_learn while client-side tools
    # are gone.
    names = _offered_names("study_learn", include_client_side=False)
    assert "quiz.generate" in names
    assert "files.read" not in names  # client-side dropped for a temporary chat


# ============================ QuizGenerateArgs server-side validation ============================
def test_quiz_args_valid_roundtrips() -> None:
    out = validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS))
    assert out["question"] == "What is 2 + 2?"
    assert out["options"] == ["3", "4", "5"]
    assert out["correctIndex"] == 1
    assert out["explanation"] == "2 + 2 = 4."


def test_quiz_args_correct_index_out_of_range_rejected() -> None:
    bad = dict(_VALID_QUIZ_ARGS, options=["a", "b"], correctIndex=2)  # valid indices are 0,1
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", bad)


def test_quiz_args_negative_correct_index_rejected() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, correctIndex=-1))


@pytest.mark.parametrize("bool_value", [True, False])
def test_quiz_args_boolean_correct_index_rejected(bool_value: bool) -> None:
    # ADR-057 §3: a JSON `true`/`false` for correctIndex is nonsense and MUST be rejected, not
    # silently coerced to 1/0. bool is an int subclass and pydantic coerces bool→int before any
    # mode="after" validator, so QuizGenerateArgs guards it in a dedicated mode="before" validator
    # (``_reject_bool_index``) on the RAW input. The raised ValueError → ValidationError → the
    # ADR-057 degrade path (content-free ``invalid_quiz`` tool_result), never a coerced index.
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, correctIndex=bool_value))


def test_quiz_args_too_few_options_rejected() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(
            "quiz.generate", dict(_VALID_QUIZ_ARGS, options=["only-one"], correctIndex=0)
        )


def test_quiz_args_too_many_options_rejected() -> None:
    eleven = [f"opt-{i}" for i in range(11)]
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, options=eleven, correctIndex=0))


def test_quiz_args_overlong_question_rejected() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, question="q" * 1001))


def test_quiz_args_overlong_option_rejected() -> None:
    bad = dict(_VALID_QUIZ_ARGS, options=["ok", "x" * 401], correctIndex=0)
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", bad)


def test_quiz_args_overlong_explanation_rejected() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, explanation="e" * 2001))


def test_quiz_args_extra_field_rejected() -> None:
    # extra='forbid' (strict): additionalProperties:false server-side, an unknown key is rejected.
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", dict(_VALID_QUIZ_ARGS, sneaky="x"))


def test_quiz_args_missing_field_rejected() -> None:
    # All fields required (OpenAI strict-mode: no optional properties).
    incomplete = {"question": "q", "options": ["a", "b"], "correctIndex": 0}  # no explanation
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", incomplete)


# ============================ QuizSchema response-boundary validator ============================
def test_quiz_schema_valid() -> None:
    quiz = QuizSchema.model_validate(dict(_VALID_QUIZ_ARGS))
    assert quiz.correctIndex == 1
    assert quiz.options == ["3", "4", "5"]


def test_quiz_schema_correct_index_ge_len_options_rejected() -> None:
    # ADR-057 §5: QuizSchema re-checks correctIndex < len(options) at the response boundary.
    with pytest.raises(ValidationError):
        QuizSchema.model_validate(dict(_VALID_QUIZ_ARGS, options=["a", "b"], correctIndex=2))


def test_quiz_schema_negative_correct_index_rejected() -> None:
    # ge=0 on the field: a negative index never reaches the client.
    with pytest.raises(ValidationError):
        QuizSchema.model_validate(dict(_VALID_QUIZ_ARGS, correctIndex=-1))

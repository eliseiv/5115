"""Unit tests for the Study & Learn quiz.generate tool contract (pure, no I/O).

ADR-057 introduced the quiz tool; **ADR-062 redesigns it into a POOL** — ``quiz.generate`` now takes
``{questions: [ {question, options, correctIndex, explanation}, ... ]}`` (3..10 questions), the
per-question card is a nested strict submodel, and ``QuizSchema`` mirrors the pool shape. These unit
tests exercise the load-bearing invariants of the pooled strict function-tool:

- the OpenAI-strict wire schema for quiz.generate carries ``strict: true`` and every keyword
  POSITION in it is a member of the ``_OPENAI_STRICT_ALLOWED_KEYWORDS`` allowlist — the STRUCTURAL
  guard against the provider ``400 Invalid schema`` blocker (ADR-062 §5). The nested list serializes
  through ``$defs``/``$ref``/``items``/``type: array`` (all allowlisted, they survive) while the
  count/length keywords (``minItems``/``maxItems``/``minLength``/``maxLength``) are stripped;
- the nested per-question submodel record in ``$defs`` is itself strict:
  ``additionalProperties: false`` with every property in ``required`` (ADR-062 §5 — else the
  provider 400s each turn);
- non-strict tools are serialized UNCHANGED (their length constraints survive);
- ``_clean_for_openai_strict`` is a pure, SCHEMA-AWARE function (no input mutation; keeps only
  allowlisted keyword positions; preserves property/$defs NAMES incl. ones named like keywords);
- the provider-NEUTRAL schema keeps the numeric constraints on BOTH the pool (``questions`` 3..10)
  and the nested question (``options`` 2..10, length caps) — the server-side validation source;
- the dialog-mode gate: quiz.generate offered ONLY under study_learn, composing with the temporary
  (include_client_side=False) axis;
- ``QuizGenerateArgs`` server-side validation of the pool: count bounds + all-or-nothing over any
  nested-question violation (ADR-062 §7 — the SOLE enforcer of invariants strict cannot express);
- ``QuizSchema`` response-boundary validator (pool count + per-question ``correctIndex <
  len(options)``).

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

# One valid quiz question card (nested element of the pool).
_VALID_CARD: dict[str, Any] = {
    "question": "What is 2 + 2?",
    "options": ["3", "4", "5"],
    "correctIndex": 1,
    "explanation": "2 + 2 = 4.",
}


def _card(**overrides: Any) -> dict[str, Any]:
    base = {
        "question": "What is 2 + 2?",
        "options": ["3", "4", "5"],
        "correctIndex": 1,
        "explanation": "2 + 2 = 4.",
    }
    base.update(overrides)
    return base


def _pool(n: int = 3, *, bad: dict[str, Any] | None = None) -> dict[str, Any]:
    """A valid pool of ``n`` cards; ``bad`` (if given) REPLACES the last card (all-or-nothing)."""
    cards = [_card() for _ in range(n)]
    if bad is not None:
        cards[-1] = bad
    return {"questions": cards}


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


def _nested_question_defs(params: dict[str, Any]) -> dict[str, Any]:
    """The single nested per-question ``$defs`` record (ADR-062 §5) inside a quiz wire schema."""
    defs = params["$defs"]
    assert len(defs) == 1, f"expected exactly one nested question def, got {sorted(defs)}"
    return next(iter(defs.values()))


# =================== static guard against the strict-400 blocker ===================
def test_openai_quiz_schema_keyword_positions_are_allowlist_subset() -> None:
    # ADR-062 §5 (the blocker guard, INVERTED to an allowlist): quiz.generate is serialized strict,
    # and EVERY keyword position in its POOLED wire schema (wrapper + nested $defs question) must be
    # a member of the allowlist. This is the structural guarantee a denylist could not give — an
    # unknown/unsupported keyword (title, maxLength, minItems, allOf, …) is DROPPED, never forwarded
    # to 400 the request. The walk is schema-aware (field NAMES under properties/$defs are not
    # keyword positions).
    quiz_def = _openai_quiz_def()
    assert quiz_def["strict"] is True
    present = _schema_keywords(quiz_def["parameters"])
    extra = present - set(_OPENAI_STRICT_ALLOWED_KEYWORDS)
    assert extra == set(), f"strict schema carries non-allowlisted keywords: {sorted(extra)}"
    # Spot-check the classic offenders that used to be denylisted are indeed absent — including the
    # count keywords the pool would otherwise emit on `questions` (minItems/maxItems).
    assert "title" not in present
    assert "maxLength" not in present
    assert "minLength" not in present
    assert "minItems" not in present
    assert "maxItems" not in present


def test_openai_quiz_schema_keeps_strict_required_structure() -> None:
    # Structural sanity: strict-mode REQUIRES an object with every property in `required` and
    # additionalProperties:false (ADR-062 §5) — on BOTH the wrapper AND the nested question. The
    # pool wrapper carries a single `questions` array whose items $ref the nested strict question.
    params = _openai_quiz_def()["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["questions"]
    assert params["additionalProperties"] is False
    questions = params["properties"]["questions"]
    assert questions["type"] == "array"
    # The array items reference the nested question submodel (serialized via $defs/$ref, §5).
    assert "$ref" in questions["items"]

    # ADR-062 §5: the nested question record is itself strict — additionalProperties:false with
    # every property in `required` (else the provider 400s each turn).
    question_def = _nested_question_defs(params)
    assert question_def["type"] == "object"
    assert question_def["additionalProperties"] is False
    assert set(question_def["required"]) == {
        "question",
        "options",
        "correctIndex",
        "explanation",
    }
    assert set(question_def["properties"]) == {
        "question",
        "options",
        "correctIndex",
        "explanation",
    }
    # Types survive the strip (constraints stripped, structure intact).
    assert question_def["properties"]["options"]["type"] == "array"
    assert question_def["properties"]["options"]["items"]["type"] == "string"


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
    # ADR-062 §5: the clean is a PURE function — the same neutral def can be serialized for other
    # providers afterwards. Feed it the real POOLED neutral quiz schema; assert the input is
    # byte-identical after the call (deep-copy compare) and the result is a distinct object.
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
    # The cleaned copy actually reduced to the allowlist (proves it worked, not a no-op) — the
    # nested $defs question constraints are stripped too.
    assert _schema_keywords(cleaned) <= set(_OPENAI_STRICT_ALLOWED_KEYWORDS)
    assert "minItems" not in _schema_keywords(cleaned)
    assert "maxItems" not in _schema_keywords(cleaned)
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
    # ADR-062 §5: the constraints stay in the NEUTRAL schema (server-side validation source); only
    # the OpenAI wire copy is stripped. Anthropic and validate_tool_args rely on these — on BOTH the
    # pool count (questions 3..10) AND the nested question (options 2..10, length caps).
    neutral = next(
        d
        for d in neutral_tool_definitions(dialog_mode="study_learn")
        if d["name"] == "quiz.generate"
    )
    schema = neutral["input_schema"]
    questions = schema["properties"]["questions"]
    assert questions["minItems"] == 3
    assert questions["maxItems"] == 10
    # The nested question submodel constraints live under $defs (referenced by items.$ref).
    question_def = _nested_question_defs(schema)
    props = question_def["properties"]
    assert props["options"]["minItems"] == 2
    assert props["options"]["maxItems"] == 10
    assert props["question"]["maxLength"] == 1000
    assert props["options"]["items"]["maxLength"] == 400
    assert props["explanation"]["maxLength"] == 2000


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
    # ADR-056 + ADR-062: quiz.generate is a GLOBAL server-side tool, so include_client_side=False
    # (temporary chat) does NOT drop it; it stays offered under study_learn while client-side tools
    # are gone.
    names = _offered_names("study_learn", include_client_side=False)
    assert "quiz.generate" in names
    assert "files.read" not in names  # client-side dropped for a temporary chat


# ================= QuizGenerateArgs server-side validation (pool) =================
def test_quiz_args_valid_pool_of_three_roundtrips() -> None:
    out = validate_tool_args("quiz.generate", _pool(3))
    assert isinstance(out["questions"], list)
    assert len(out["questions"]) == 3
    q0 = out["questions"][0]
    assert q0["question"] == "What is 2 + 2?"
    assert q0["options"] == ["3", "4", "5"]
    assert q0["correctIndex"] == 1
    assert q0["explanation"] == "2 + 2 = 4."


def test_quiz_args_valid_pool_of_ten_roundtrips() -> None:
    out = validate_tool_args("quiz.generate", _pool(10))
    assert len(out["questions"]) == 10


@pytest.mark.parametrize("n", [0, 1, 2, 11, 12])
def test_quiz_args_pool_count_out_of_bounds_rejected(n: int) -> None:
    # ADR-062 §1/§7: the pool must carry 3..10 questions (empty list included). Anything outside the
    # range is a single ValidationError → the whole pool rejected.
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", _pool(n) if n else {"questions": []})


def test_quiz_args_pool_boundaries_three_and_ten_accepted() -> None:
    # The inclusive boundaries pass (3 and 10).
    assert len(validate_tool_args("quiz.generate", _pool(3))["questions"]) == 3
    assert len(validate_tool_args("quiz.generate", _pool(10))["questions"]) == 10


# --- all-or-nothing over a single bad nested question (ADR-062 §7) ---
@pytest.mark.parametrize(
    "bad_card",
    [
        pytest.param(_card(options=["a", "b"], correctIndex=2), id="correct_index_out_of_range"),
        pytest.param(_card(correctIndex=-1), id="negative_correct_index"),
        pytest.param(_card(correctIndex=True), id="boolean_correct_index"),
        pytest.param(_card(options=["only-one"], correctIndex=0), id="too_few_options"),
        pytest.param(
            _card(options=[f"opt-{i}" for i in range(11)], correctIndex=0), id="too_many_options"
        ),
        pytest.param(_card(question="q" * 1001), id="overlong_question"),
        pytest.param(_card(options=["ok", "x" * 401], correctIndex=0), id="overlong_option"),
        pytest.param(_card(explanation="e" * 2001), id="overlong_explanation"),
        pytest.param({**_card(), "sneaky": "x"}, id="extra_field"),
        pytest.param(
            {"question": "q", "options": ["a", "b"], "correctIndex": 0}, id="missing_explanation"
        ),
    ],
)
def test_quiz_args_single_bad_question_rejects_whole_pool(bad_card: dict[str, Any]) -> None:
    # ADR-062 §7 all-or-nothing: ONE invalid question among otherwise-valid ones invalidates the
    # ENTIRE pool (no partial acceptance). The wrapper+list is one pydantic model → one
    # ValidationError. The bad card sits at the LAST position amid two valid ones.
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", _pool(3, bad=bad_card))


def test_quiz_args_boolean_false_index_rejected() -> None:
    # ADR-062 §7 + ADR-057 §3: a JSON `false` for correctIndex is nonsense and MUST NOT be coerced
    # to index 0 — the dedicated mode="before" guard rejects it on the RAW input.
    with pytest.raises(ValueError):
        validate_tool_args("quiz.generate", _pool(3, bad=_card(correctIndex=False)))


# ================= QuizSchema response-boundary validator (pool) =================
def test_quiz_schema_valid_pool() -> None:
    quiz = QuizSchema.model_validate(_pool(3))
    assert len(quiz.questions) == 3
    assert quiz.questions[0].correctIndex == 1
    assert quiz.questions[0].options == ["3", "4", "5"]


def test_quiz_schema_per_question_correct_index_ge_len_options_rejected() -> None:
    # ADR-062 §2: QuizSchema re-checks correctIndex < len(options) for EACH question at the response
    # boundary — one out-of-range question fails the whole pool.
    with pytest.raises(ValidationError):
        QuizSchema.model_validate(_pool(3, bad=_card(options=["a", "b"], correctIndex=2)))


def test_quiz_schema_per_question_negative_correct_index_rejected() -> None:
    # ge=0 on the field: a negative index in ANY question never reaches the client.
    with pytest.raises(ValidationError):
        QuizSchema.model_validate(_pool(3, bad=_card(correctIndex=-1)))


@pytest.mark.parametrize("n", [0, 2, 11])
def test_quiz_schema_pool_count_out_of_bounds_rejected(n: int) -> None:
    # ADR-062 §2: QuizSchema mirrors the 3..10 pool bound at the response boundary.
    payload = {"questions": []} if n == 0 else _pool(n)
    with pytest.raises(ValidationError):
        QuizSchema.model_validate(payload)

"""Unit tests for ADR-022: optional projectId + axis-A site.* gating by project presence.

Pure, no I/O. Two concerns:
- ChatRunRequest.projectId validator: optional (None ok), but a present-yet-blank value is 422.
- anthropic_tool_definitions(include_server_side=...) drops SERVER_SIDE_TOOLS (site.*) when False
  while keeping every client-side tool (ADR-063: none shipped — exercised via a test-only fake
  client tool), and the full non-quiz set (incl. site.*) when True. The dialog-mode-gated
  quiz.generate (ADR-057 §4) is offered ONLY under dialog_mode="study_learn" and composes with the
  axis-A project gate by logical AND.

Axis B (assistant_mode) is intentionally NOT exercised: per the task and tools.py docstring it is
Q-012-1 Open and NOT implemented — the only code-level gate today is project_id (axis A).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.chat.tools import (
    ALL_TOOL_NAMES,
    SERVER_SIDE_TOOLS,
    TOOL_QUIZ_GENERATE,
    anthropic_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
)
from app.schemas.chat import ChatRunRequest

# ADR-057 §4: quiz.generate is dialog-mode-gated — offered ONLY when dialog_mode == "study_learn".
# So the DEFAULT offer-set (no dialog_mode) is ALL_TOOL_NAMES minus quiz.generate; it re-enters the
# set only under study_learn. Axis-A (project presence) and this gate compose by logical AND.
_NON_QUIZ_TOOLS = set(ALL_TOOL_NAMES) - {TOOL_QUIZ_GENERATE}

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _run_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"userId": str(_UID), "message": "hi", "mode": "credits"}
    base.update(overrides)
    return base


# ----------------------------- projectId validator (scenario 1) -----------------------------
def test_run_request_without_project_id_is_valid_and_none() -> None:
    req = ChatRunRequest.model_validate(_run_payload())
    assert req.projectId is None


def test_run_request_with_project_id_is_valid() -> None:
    req = ChatRunRequest.model_validate(_run_payload(projectId="proj-1"))
    assert req.projectId == "proj-1"


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t \n "])
def test_run_request_blank_project_id_rejected(blank: str) -> None:
    # ADR-022 §1: present-but-blank projectId is a 422 (not silently coerced to NULL).
    with pytest.raises(ValidationError, match="non-empty"):
        ChatRunRequest.model_validate(_run_payload(projectId=blank))


# ----------------------------- axis-A tool gating (scenario 2) -----------------------------
def _domain_names(*, include_server_side: bool, dialog_mode: str | None = None) -> set[str]:
    defs = anthropic_tool_definitions(
        include_server_side=include_server_side, dialog_mode=dialog_mode
    )
    # definitions carry the anthropic wire (underscore) names — reverse-map to domain for asserts.
    return {to_domain_tool_name(d["name"]) for d in defs}


def test_definitions_with_project_include_all_non_quiz_tools() -> None:
    # Default dialog_mode (None ≠ study_learn) → quiz.generate is NOT offered (ADR-057 §4); the rest
    # of the catalog IS. So the offered set is ALL_TOOL_NAMES minus the dialog-gated quiz.generate.
    # ADR-058: image.generate is offered because OPENAI_API_KEY is set in the test env (conftest
    # forces a non-empty placeholder — the key-gate is satisfied), so it counts among the non-quiz
    # tools here.
    names = _domain_names(include_server_side=True)
    assert names == _NON_QUIZ_TOOLS
    # ADR-063: 5 site.* + time.now + image.generate = 7 (quiz.generate dialog-gated out).
    assert len(names) == 7
    # site.* present; quiz.generate absent until study_learn.
    assert names >= SERVER_SIDE_TOOLS
    assert TOOL_QUIZ_GENERATE not in names


def test_definitions_with_project_and_study_learn_add_quiz() -> None:
    # ADR-057 §4: under study_learn the global server-side quiz.generate re-enters the offered set,
    # composing with the project (axis-A) gate → the FULL catalog incl. quiz.generate and site.*.
    names = _domain_names(include_server_side=True, dialog_mode="study_learn")
    assert names == set(ALL_TOOL_NAMES)
    assert TOOL_QUIZ_GENERATE in names
    assert names >= SERVER_SIDE_TOOLS


def test_definitions_without_project_exclude_site_tools() -> None:
    names = _domain_names(include_server_side=False)
    # No site.* at all.
    assert names.isdisjoint(SERVER_SIDE_TOOLS)
    # Complement of project-scoped site.* AND the dialog-gated quiz.generate (ADR-026: time.now
    # stays — global, not gated by project; ADR-058: image.generate stays — global, key-gate
    # satisfied; ADR-057: quiz.generate needs study_learn).
    assert names == _NON_QUIZ_TOOLS - set(SERVER_SIDE_TOOLS)
    # ADR-063: time.now + image.generate (quiz.generate gated out; site.* dropped; no client-side).
    assert len(names) == 2
    assert "time.now" in names
    assert "image.generate" in names
    assert TOOL_QUIZ_GENERATE not in names


def test_definitions_without_project_but_study_learn_add_quiz_keep_no_site() -> None:
    # The dialog-mode gate composes with axis-A: study_learn re-adds the GLOBAL quiz.generate even
    # in a project-less «чистый чат», while project-scoped site.* stay excluded.
    names = _domain_names(include_server_side=False, dialog_mode="study_learn")
    assert names.isdisjoint(SERVER_SIDE_TOOLS)
    assert TOOL_QUIZ_GENERATE in names
    # ADR-063: time.now + quiz.generate + image.generate (ADR-058, key-gate satisfied).
    assert len(names) == 3
    assert "image.generate" in names


def test_definitions_without_project_keep_client_side_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-022 §2: client-side tools are NOT touched by the project (axis-A) gate. ADR-063 removed
    # all shipped client-side tools, so a test-only fake client tool stands in as the example: it
    # must still be offered when include_server_side=False (site.* dropped, client-side kept).
    from tests.fake_client_tool import register_fake_client_tool

    fake = register_fake_client_tool(monkeypatch)
    names = _domain_names(include_server_side=False)
    assert fake in names
    assert names.isdisjoint(SERVER_SIDE_TOOLS)


def test_default_include_server_side_is_true() -> None:
    # Backwards-compatible default: omitting the flag keeps the full set (pre-ADR-022 behavior),
    # EXCEPT the dialog-gated quiz.generate which is offered only under study_learn (ADR-057 §4).
    assert {to_domain_tool_name(d["name"]) for d in anthropic_tool_definitions()} == _NON_QUIZ_TOOLS


def test_emitted_names_are_wire_underscore_form() -> None:
    # Whichever gate, emitted names are always the underscore wire form (BUG-3), no dots.
    for flag in (True, False):
        defs = anthropic_tool_definitions(include_server_side=flag)
        names = {d["name"] for d in defs}
        assert all("." not in n for n in names)
        # Each emitted name reverse-maps to a known domain tool (bijective on the offered subset).
        assert names == {to_anthropic_tool_name(to_domain_tool_name(n)) for n in names}

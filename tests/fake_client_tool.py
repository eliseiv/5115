"""Shared TEST-ONLY fake client-side tool (ADR-063).

After ADR-063 no real client-side tool is registered in the shipped catalog — the 8 remaining
tools are all server-side (5 ``site.*`` + ``time.now`` / ``quiz.generate`` / ``image.generate``).
The client-side PROTOCOL is kept dormant (``POST /v1/chat/tool-result``, ``toolCalls[]``, the turn
barrier of ADR-025, ``_is_client_side`` / ``include_client_side`` of ADR-056), but there is no
in-catalog example to exercise it with.

Tests that need an EXAMPLE client-side tool (the turn barrier + parallel tool_use of ADR-025, the
``include_client_side`` gate of ADR-056, history domain-normalization of ADR-024, sync ids of
ADR-023, mixed server/client turns of ADR-028/030, ...) register a fake one here — ONLY inside the
test process, never shipped. It is added to the module-level registries via ``monkeypatch``
(auto-restored at teardown, no leak across tests), lives in neither ``SERVER_SIDE_TOOLS`` nor
``GLOBAL_SERVER_SIDE_TOOLS`` (so ``tools._is_client_side`` classifies it client-side), and uses a
permissive args model (``extra='allow'`` → any argument dict round-trips through
``validate_tool_args`` unchanged, mirroring the removed ``files.*`` / ``calendar.*`` tools).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from app.chat import tools as tools_mod

# Domain (dotted) names + their Anthropic underscore wire names. Deliberately-hypothetical
# placeholders (there is no shipped client-side tool by these names).
FAKE_CLIENT_TOOL = "example.client_tool"
FAKE_CLIENT_TOOL_WIRE = "example_client_tool"
FAKE_CLIENT_MUTATING_TOOL = "example.client_create"
FAKE_CLIENT_MUTATING_TOOL_WIRE = "example_client_create"


class ExampleClientToolArgs(BaseModel):
    """Permissive args for the fake client-side tool.

    ``extra='allow'`` so an arbitrary argument dict validates and round-trips through
    ``validate_tool_args`` (``model_dump()``) byte-for-byte — the fake tool stands in for the
    removed ``files.*`` / ``calendar.*`` tools without pinning their schemas.
    """

    model_config = ConfigDict(extra="allow")


def register_fake_client_tool(monkeypatch: pytest.MonkeyPatch, *, mutating: bool = False) -> str:
    """Register the fake client-side tool into the ``tools`` registries for one test.

    Uses ``monkeypatch.setitem`` / ``setattr`` so every registry mutation is auto-restored at
    teardown. Returns the domain (dotted) name to use in the test. Pass ``mutating=True`` for the
    variant that is in ``MUTATING_TOOLS`` (an audited, mutating client-side tool).
    """
    name = FAKE_CLIENT_MUTATING_TOOL if mutating else FAKE_CLIENT_TOOL
    wire = FAKE_CLIENT_MUTATING_TOOL_WIRE if mutating else FAKE_CLIENT_TOOL_WIRE
    monkeypatch.setitem(tools_mod._ARGS_BY_TOOL, name, ExampleClientToolArgs)
    monkeypatch.setitem(tools_mod.TOOL_DESCRIPTIONS, name, "Example client-side tool (test only).")
    monkeypatch.setitem(tools_mod._DOMAIN_TO_ANTHROPIC, name, wire)
    monkeypatch.setitem(tools_mod._ANTHROPIC_TO_DOMAIN, wire, name)
    if mutating:
        # MUTATING_TOOLS is a frozenset (immutable) — it cannot be mutated in place like the dict
        # registries above. It is also imported BY VALUE (``from app.chat.tools import
        # MUTATING_TOOLS``) into the orchestrator, so a single setattr on the tools module would
        # not be seen there. Rebind the SAME new frozenset in every module that binds the name
        # (auto-restored by monkeypatch at teardown).
        from app.chat import orchestrator as orchestrator_mod

        new_mutating = tools_mod.MUTATING_TOOLS | {name}
        monkeypatch.setattr(tools_mod, "MUTATING_TOOLS", new_mutating)
        monkeypatch.setattr(orchestrator_mod, "MUTATING_TOOLS", new_mutating)
    return name

"""E2E against the REAL Anthropic API: ``time.now`` fixes the «model thinks it's 2024» bug.

Purpose (ADR-026 / 06-testing-strategy.md §time.now e2e row): in a «чистый чат» (no project) a
date question must drive Claude to call the global server-side ``time.now`` tool and answer with
the CORRECT current date (2026, not «2024» guessed from the training corpus). This exercises the
real tool-loop end to end against the live Messages API.

STATUS: BLOCKED by an EXTERNAL account issue, NOT by the code. The configured production Anthropic
key belongs to a DISABLED organization (generation blocked — see MEMORY / deployment-state), so a
live call returns `400: "This organization has been disabled."`. The test is therefore SKIPPED in
CI and on local runs and kept as the executable spec for when a working key is available. The
``time.now`` behaviour itself (UTC set, tz, routing, billing, prompt invariant) is fully covered
hermetically in tests/unit/test_time_now_tool.py, tests/unit/test_time_now_registry.py and
tests/integration/test_time_now_tool_loop_adr026.py. Run with `-m external` once the org is
re-enabled (set ANTHROPIC_API_KEY_E2E to a working key).
"""

from __future__ import annotations

import datetime
import os

import pytest

_SKIP_REASON = (
    "Blocked by external account: the production Anthropic key belongs to a DISABLED organization "
    "(400 'This organization has been disabled.'). Not a code failure (ADR-026). Re-enable the org "
    "or supply a working ANTHROPIC_API_KEY_E2E, then run with `-m external`."
)


@pytest.mark.external
@pytest.mark.skip(reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_real_anthropic_time_now_returns_current_year_not_2024() -> None:
    """Ask the current year in a clean chat → Claude calls time.now → answer mentions 2026."""
    from app.chat.anthropic_client import AnthropicClient
    from app.chat.global_tools import GlobalToolHandlers
    from app.chat.orchestrator import _system_prompt_for
    from app.chat.tools import (
        anthropic_tool_definitions,
        to_anthropic_tool_name,
        to_domain_tool_name,
        validate_tool_args,
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY_E2E") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("no ANTHROPIC_API_KEY for the external e2e")

    handlers = GlobalToolHandlers()  # real SystemClock
    current_year = str(datetime.datetime.now(tz=datetime.UTC).year)

    client = AnthropicClient()
    # «Чистый чат» — no project → site.* excluded, time.now still offered (ADR-026 §3).
    tools = anthropic_tool_definitions(include_server_side=False)
    assert to_anthropic_tool_name("time.now") in {t["name"] for t in tools}

    messages: list[dict] = [
        {"role": "user", "content": "What year is it right now? Answer with the year."}
    ]
    system_prompt = _system_prompt_for("chat")

    # Bounded tool-loop: Claude should call time.now, then answer with the real year.
    final_text = ""
    for _ in range(4):
        result = await client.create_message(
            system_prompt=system_prompt, messages=messages, tools=tools, api_key=api_key
        )
        if result.stop_reason == "tool_use" and result.tool_uses:
            messages.append({"role": "assistant", "content": result.content_blocks})
            tool_results = []
            for block in result.tool_uses:
                domain_name = to_domain_tool_name(str(block["name"]))
                args = validate_tool_args(domain_name, dict(block["input"]))
                execution = await handlers.execute(tool_name=domain_name, args=args)
                import json

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(block["id"]),
                        "content": json.dumps(execution.to_tool_result_payload().get("result")),
                        "is_error": execution.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue
        final_text = result.text
        break

    assert current_year == "2026"  # the bug reporter's premise: real date is 2026
    assert current_year in final_text, final_text
    assert "2024" not in final_text, final_text

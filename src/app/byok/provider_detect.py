"""BYOK provider detection by API-key prefix (ADR-044 §1).

Single source of truth for deciding which LLM provider a BYOK key belongs to, by inspecting only
its KNOWN prefix — no regex on the key body, no network probing. The returned value is restricted
to the canonical provider set ``{"anthropic", "openai"}`` (same set as ``LLM_PROVIDER``); ``None``
means the format is not recognized.

Security (ADR-044 §8 / 05-security.md): this function is PURE — it neither logs the key nor raises.
The key is normalized only with ``strip()`` (leading/trailing whitespace); the body is never
transformed.
"""

from __future__ import annotations

# Canonical provider names returned by detection (subset of LLM_PROVIDER values). A new provider is
# added here AND in the llm_client_for factory (ADR-044 §2) — one shared set.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"


def detect_byok_provider(api_key: str) -> str | None:
    """Detect the BYOK provider from the key prefix (ADR-044 §1). Pure: no logging, no raise.

    Order is strict and load-bearing (``sk-ant-`` BEFORE ``sk-``): an Anthropic key also starts
    with ``sk-``, so the Anthropic check MUST precede the OpenAI check or it would be misclassified.

    | Priority | Condition (after ``strip()``) | Result        |
    |----------|-------------------------------|---------------|
    | 1        | starts with ``sk-ant-``       | ``"anthropic"`` |
    | 2        | starts with ``sk-proj-``      | ``"openai"``    |
    | 3        | starts with ``sk-``           | ``"openai"``    |
    | 4        | otherwise                     | ``None``        |

    ``sk-proj-`` (OpenAI project-scoped) is a special case of OpenAI; the explicit branch documents
    intent (priorities 2 and 3 yield the same ``"openai"``). An unrecognized format → ``None`` (the
    caller treats it as a terminal ``invalid`` status without any network call — ADR-044 §3.1).
    """
    key = api_key.strip()
    if key.startswith("sk-ant-"):
        return PROVIDER_ANTHROPIC
    if key.startswith("sk-proj-"):
        return PROVIDER_OPENAI
    if key.startswith("sk-"):
        return PROVIDER_OPENAI
    return None

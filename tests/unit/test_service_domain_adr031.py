"""Unit: Settings.normalized_service_domain() — ADR-031 absolute preview URL host.

Pure (no I/O). Verifies the bare host[:port] normalization used to build the absolute
site.preview URL: scheme stripping (case-insensitive), surrounding-slash snapping, and the
"not configured" empty result that the caller maps to the relative fallback. No app/DB needed.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _domain(raw: str) -> str:
    # Construct an isolated Settings (no cache) so SERVICE_DOMAIN is exactly `raw`.
    return Settings(SERVICE_DOMAIN=raw).normalized_service_domain()


@pytest.mark.parametrize(
    "raw",
    [
        "broadnova.shop",
        "https://broadnova.shop",
        "HTTP://broadnova.shop/",
        "  broadnova.shop/  ",
        "https://broadnova.shop/",
        "http://broadnova.shop",
        "//broadnova.shop//",
    ],
)
def test_normalizes_to_bare_host(raw: str) -> None:
    assert _domain(raw) == "broadnova.shop"


def test_preserves_port() -> None:
    # A host:port stays intact (scheme/slashes only are stripped).
    assert _domain("https://broadnova.shop:8443/") == "broadnova.shop:8443"


@pytest.mark.parametrize("raw", ["", "   ", "/", "//", "  /  "])
def test_blank_or_only_slashes_yields_empty(raw: str) -> None:
    # Empty => caller treats as "not configured" => relative fallback (no host invented).
    assert _domain(raw) == ""


def test_default_is_empty() -> None:
    # Unset SERVICE_DOMAIN defaults to '' (dev posture => relative fallback).
    assert Settings(SERVICE_DOMAIN="").normalized_service_domain() == ""

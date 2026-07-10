"""Unit tests for trusted-proxy client-IP resolution (deps.client_ip, 05-security.md).

Security-critical: X-Forwarded-For / X-Real-IP are honoured ONLY when the socket peer is a
configured trusted proxy. Otherwise the headers are attacker-controlled and ignored. From a
trusted chain the client is taken (hop_count + 1) from the right — never the spoofable
left-most entry. Covers backend follow_up_for_qa for iteration 4.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import deps
from app.config import Settings


class _FakeAddr:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for starlette Request: only .client.host and .headers are read."""

    def __init__(self, peer: str | None, headers: dict[str, str] | None = None) -> None:
        self.client = _FakeAddr(peer) if peer is not None else None
        # client_ip reads headers case-insensitively via .get("x-forwarded-for") etc.;
        # starlette Headers is case-insensitive, so normalise keys to lower here.
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _settings(trusted: str = "", hop_count: int = 1) -> Settings:
    return Settings(TRUSTED_PROXY_IPS=trusted, TRUSTED_PROXY_HOP_COUNT=hop_count)


@pytest.fixture
def patch_settings(monkeypatch: pytest.MonkeyPatch):
    def _apply(trusted: str = "", hop_count: int = 1) -> None:
        s = _settings(trusted, hop_count)
        monkeypatch.setattr(deps, "get_settings", lambda: s)

    return _apply


def _client_ip(peer: str | None, headers: dict[str, str] | None = None) -> Any:
    return deps.client_ip(_FakeRequest(peer, headers))


def test_untrusted_peer_xff_ignored_returns_peer(patch_settings: Any) -> None:
    # Empty trusted list => never trust forwarding headers; spoofed XFF must be ignored.
    patch_settings(trusted="")
    ip = _client_ip("203.0.113.7", {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
    assert ip == "203.0.113.7"


def test_spoofed_xff_with_empty_trusted_list_uses_socket_peer(patch_settings: Any) -> None:
    # Even with X-Real-IP an attacker could set, an untrusted peer means we trust only the peer.
    patch_settings(trusted="")
    ip = _client_ip("198.51.100.9", {"X-Real-IP": "10.0.0.1"})
    assert ip == "198.51.100.9"


def test_peer_not_in_trusted_cidr_ignores_xff(patch_settings: Any) -> None:
    # Trusted proxies are 10.0.0.0/8 but the peer is public => headers untrusted.
    patch_settings(trusted="10.0.0.0/8")
    ip = _client_ip("203.0.113.7", {"X-Forwarded-For": "1.2.3.4"})
    assert ip == "203.0.113.7"


def test_trusted_proxy_takes_rightmost_non_trusted_hop(patch_settings: Any) -> None:
    # peer 10.0.0.5 is the trusted proxy that appended the right-most XFF entry (9.9.9.9).
    # hop_count=1 => index = len(2) - 1 - 1 = 0 => the client is the left-most real address.
    patch_settings(trusted="10.0.0.0/8", hop_count=1)
    ip = _client_ip("10.0.0.5", {"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
    assert ip == "1.1.1.1"


def test_trusted_proxy_multi_hop_drops_two_rightmost(patch_settings: Any) -> None:
    # Two trusted hops recorded the two right-most XFF entries (7.7.7.7, 6.6.6.6).
    # hop_count=2 => index = len(3) - 2 - 1 = 0 => client is the left-most real address.
    patch_settings(trusted="10.0.0.0/8", hop_count=2)
    ip = _client_ip("10.0.0.5", {"X-Forwarded-For": "8.8.8.8, 7.7.7.7, 6.6.6.6"})
    assert ip == "8.8.8.8"


def test_trusted_proxy_single_hop_drops_only_rightmost(patch_settings: Any) -> None:
    # Longer chain, one trusted hop => take the entry just left of the right-most.
    # hop_count=1 => index = len(3) - 1 - 1 = 1.
    patch_settings(trusted="10.0.0.0/8", hop_count=1)
    ip = _client_ip("10.0.0.5", {"X-Forwarded-For": "8.8.8.8, 7.7.7.7, 6.6.6.6"})
    assert ip == "7.7.7.7"


def test_trusted_proxy_hop_count_exceeds_chain_clamps_to_leftmost(patch_settings: Any) -> None:
    # If hop_count is larger than the chain, index clamps to 0 (left-most), never negative.
    patch_settings(trusted="10.0.0.0/8", hop_count=5)
    ip = _client_ip("10.0.0.5", {"X-Forwarded-For": "4.4.4.4, 3.3.3.3"})
    assert ip == "4.4.4.4"


def test_trusted_proxy_falls_back_to_x_real_ip(patch_settings: Any) -> None:
    # No XFF present but trusted peer => honour X-Real-IP.
    patch_settings(trusted="10.0.0.0/8")
    ip = _client_ip("10.0.0.5", {"X-Real-IP": "2.2.2.2"})
    assert ip == "2.2.2.2"


def test_trusted_proxy_no_forwarding_headers_returns_peer(patch_settings: Any) -> None:
    patch_settings(trusted="10.0.0.0/8")
    ip = _client_ip("10.0.0.5", {})
    assert ip == "10.0.0.5"


def test_no_socket_peer_returns_none(patch_settings: Any) -> None:
    # request.client is None (e.g. lifespan / unusual transport) => no IP to resolve.
    patch_settings(trusted="10.0.0.0/8")
    ip = _client_ip(None, {"X-Forwarded-For": "1.2.3.4"})
    assert ip is None


def test_invalid_peer_address_is_not_trusted(patch_settings: Any) -> None:
    # Unparseable peer host must never be treated as a trusted proxy.
    patch_settings(trusted="10.0.0.0/8")
    ip = _client_ip("not-an-ip", {"X-Forwarded-For": "1.2.3.4"})
    assert ip == "not-an-ip"


def test_settings_skips_invalid_cidr_entries() -> None:
    # trusted_proxy_networks() must skip malformed entries and keep valid ones.
    s = Settings(TRUSTED_PROXY_IPS="10.0.0.0/8, garbage, 192.168.1.1")
    nets = s.trusted_proxy_networks()
    assert len(nets) == 2
    rendered = {str(n) for n in nets}
    assert "10.0.0.0/8" in rendered
    assert "192.168.1.1/32" in rendered

"""Unit: preview signed URL (HMAC-SHA256 + TTL) — ADR-010, website-builder/09-testing.md.

Pure crypto/token logic, no I/O. The PREVIEW_URL_SECRET is set on the cached Settings for the
duration of each test (get_settings() is the single source the signer reads).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.website.signed_url import (
    PreviewSecretMissingError,
    build_token,
    verify_token,
)

_SECRET = "preview-secret-unit-0123456789abcdef0123456789abcdef"


@pytest.fixture
def preview_secret() -> Iterator[None]:
    settings = get_settings()
    orig_secret = settings.preview_url_secret
    orig_ttl = settings.preview_url_ttl_seconds
    settings.preview_url_secret = _SECRET
    settings.preview_url_ttl_seconds = 900
    yield
    settings.preview_url_secret = orig_secret
    settings.preview_url_ttl_seconds = orig_ttl


def test_build_verify_roundtrip_ok(preview_secret: None) -> None:
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(project_id=pid, owner_user_id=owner, now=1000)
    assert verify_token(project_id=pid, owner_user_id=owner, token=signed.token, now=1001) is True
    assert signed.expires_at == 1000 + 900


def test_tamper_project_id_rejected(preview_secret: None) -> None:
    owner = uuid.uuid4()
    signed = build_token(project_id=uuid.uuid4(), owner_user_id=owner, now=1000)
    other_pid = uuid.uuid4()
    assert (
        verify_token(project_id=other_pid, owner_user_id=owner, token=signed.token, now=1001)
        is False
    )


def test_tamper_owner_user_id_rejected(preview_secret: None) -> None:
    pid = uuid.uuid4()
    signed = build_token(project_id=pid, owner_user_id=uuid.uuid4(), now=1000)
    assert (
        verify_token(project_id=pid, owner_user_id=uuid.uuid4(), token=signed.token, now=1001)
        is False
    )


def test_tamper_exp_rejected(preview_secret: None) -> None:
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(project_id=pid, owner_user_id=owner, now=1000)
    # forge a longer exp by replacing the exp segment → HMAC no longer matches.
    import base64

    forged_exp = base64.urlsafe_b64encode(b"99999999999").rstrip(b"=").decode()
    _, mac_part = signed.token.split(".")
    forged = f"{forged_exp}.{mac_part}"
    assert verify_token(project_id=pid, owner_user_id=owner, token=forged, now=1001) is False


def test_expired_token_rejected(preview_secret: None) -> None:
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(project_id=pid, owner_user_id=owner, now=1000)  # exp=1900
    # current time after exp → expired even though HMAC is valid.
    assert verify_token(project_id=pid, owner_user_id=owner, token=signed.token, now=2000) is False


def test_at_exact_exp_still_valid(preview_secret: None) -> None:
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(project_id=pid, owner_user_id=owner, now=1000)  # exp=1900
    assert verify_token(project_id=pid, owner_user_id=owner, token=signed.token, now=1900) is True


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b.c", "@@@.###", "!!!!.notb64"])
def test_malformed_token_returns_false_not_raise(preview_secret: None, bad: str) -> None:
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    assert verify_token(project_id=pid, owner_user_id=owner, token=bad, now=1001) is False


def test_missing_secret_raises_on_build() -> None:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = ""
    try:
        with pytest.raises(PreviewSecretMissingError):
            build_token(project_id=uuid.uuid4(), owner_user_id=uuid.uuid4())
    finally:
        settings.preview_url_secret = orig

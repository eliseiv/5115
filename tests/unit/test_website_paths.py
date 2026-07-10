"""Unit: site-file path-traversal guard (ADR-010, website-builder/09-testing.md).

normalize_site_path rejects ..-traversal / absolute / backslash / NUL / empty, and normalizes
valid relative paths (collapsing '.' and duplicate slashes).
"""

from __future__ import annotations

import pytest

from app.website.paths import InvalidPathError, normalize_site_path


@pytest.mark.parametrize(
    "raw",
    [
        "../etc/passwd",
        "a/../../b",
        "..",
        "foo/..",
        "/etc/passwd",  # absolute
        "/index.html",  # absolute
        "a\\b.txt",  # backslash
        "a\\..\\b",  # backslash traversal
        "a\x00b",  # NUL
        "",  # empty
        "/",  # resolves empty / absolute
        "./",  # resolves empty
        ".",  # resolves empty
    ],
)
def test_unsafe_paths_rejected(raw: str) -> None:
    with pytest.raises(InvalidPathError):
        normalize_site_path(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("index.html", "index.html"),
        ("css/site.css", "css/site.css"),
        ("./index.html", "index.html"),
        ("a//b///c.txt", "a/b/c.txt"),
        ("a/./b.txt", "a/b.txt"),
        ("assets/img/logo.png", "assets/img/logo.png"),
    ],
)
def test_valid_paths_normalized(raw: str, expected: str) -> None:
    assert normalize_site_path(raw) == expected

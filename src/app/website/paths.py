"""Path-traversal guard for site files (ADR-010, website-builder/05-security.md).

A normalized relative path: no '..' segment, no absolute path, no backslash, no NUL byte.
Used both on write (site.write_file) and on preview read (defense-in-depth). The lookup is
always by (project_id, normalized_path) against site_files — never the filesystem.
"""

from __future__ import annotations


class InvalidPathError(ValueError):
    """Raised when a site-file path is unsafe (traversal/absolute/backslash/NUL/empty)."""


def normalize_site_path(raw: str) -> str:
    """Validate and normalize a relative site-file path. Raises InvalidPathError if unsafe.

    Rules: non-empty; no NUL; no backslash; not absolute (no leading '/'); no '..' segment.
    Collapses '.' segments and duplicate slashes to a canonical relative path. The result is the
    storage key used in site_files.path (unique per project).
    """
    if not raw:
        raise InvalidPathError("path must not be empty")
    if "\x00" in raw:
        raise InvalidPathError("path must not contain NUL")
    if "\\" in raw:
        raise InvalidPathError("path must not contain backslash")
    if raw.startswith("/"):
        raise InvalidPathError("path must be relative, not absolute")

    segments: list[str] = []
    for segment in raw.split("/"):
        if segment == "" or segment == ".":
            # Skip empty (duplicate slash / trailing slash) and current-dir segments.
            continue
        if segment == "..":
            raise InvalidPathError("path must not contain '..' traversal")
        segments.append(segment)

    if not segments:
        raise InvalidPathError("path resolves to empty")
    return "/".join(segments)

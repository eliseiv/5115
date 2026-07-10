"""Unit tests for migrations/env.py URL resolution (TD-008, 09-e2e-testing.md §3.1).

`_db_url` must prefer the URL handed in via the Alembic Config (`sqlalchemy.url`) over
`get_settings().database_url`, so e2e/testcontainers can inject an arbitrary DB without
depending on env load order. Fallback to settings only when the Config key is unset.

migrations/env.py executes migrations at import time, so it is loaded here with a stubbed
`alembic.context` (offline mode + no-op configure/run) to exercise `_db_url` in isolation.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

from app.config import get_settings

_ENV_PATH = Path(__file__).resolve().parents[2] / "migrations" / "env.py"


class _FakeConfig:
    """Minimal stand-in for Alembic's Config object used by env.py."""

    config_file_name = None
    config_ini_section = "alembic"

    def __init__(self, main_url: str | None, section_url: str | None) -> None:
        self._main_url = main_url
        self._section_url = section_url

    def get_main_option(self, name: str) -> str | None:
        if name == "sqlalchemy.url":
            return self._main_url
        return None

    def get_section(self, _name: str, default: Any = None) -> dict[str, Any]:
        section: dict[str, Any] = {}
        if self._section_url is not None:
            section["sqlalchemy.url"] = self._section_url
        return section if section else (default if default is not None else {})


def _load_env_with_config(config: _FakeConfig):
    """Import migrations/env.py with alembic.context stubbed so no real migration runs."""
    fake_context = types.ModuleType("alembic.context")
    fake_context.config = config  # type: ignore[attr-defined]
    fake_context.is_offline_mode = lambda: True  # type: ignore[attr-defined]
    fake_context.configure = lambda **_kw: None  # type: ignore[attr-defined]
    fake_context.run_migrations = lambda **_kw: None  # type: ignore[attr-defined]

    class _Tx:
        def __enter__(self) -> _Tx:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    fake_context.begin_transaction = lambda: _Tx()  # type: ignore[attr-defined]

    fake_alembic = types.ModuleType("alembic")
    fake_alembic.context = fake_context  # type: ignore[attr-defined]

    saved = {k: sys.modules.get(k) for k in ("alembic", "alembic.context")}
    sys.modules["alembic"] = fake_alembic
    sys.modules["alembic.context"] = fake_context
    try:
        spec = importlib.util.spec_from_file_location("_migrations_env_under_test", _ENV_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        sys.modules.pop("_migrations_env_under_test", None)


def test_db_url_prefers_config_main_option() -> None:
    injected = "postgresql+asyncpg://u:p@container:5432/e2e"
    module = _load_env_with_config(_FakeConfig(main_url=injected, section_url=None))
    assert module._db_url() == injected


def test_db_url_prefers_config_section_when_no_main() -> None:
    injected = "postgresql+asyncpg://u:p@section:5432/e2e"
    module = _load_env_with_config(_FakeConfig(main_url=None, section_url=injected))
    assert module._db_url() == injected


def test_db_url_falls_back_to_settings_when_config_empty() -> None:
    module = _load_env_with_config(_FakeConfig(main_url=None, section_url=None))
    assert module._db_url() == get_settings().database_url


def test_db_url_ignores_empty_string_config() -> None:
    """An empty (falsy) Config value must not shadow the settings fallback."""
    module = _load_env_with_config(_FakeConfig(main_url="", section_url=""))
    assert module._db_url() == get_settings().database_url

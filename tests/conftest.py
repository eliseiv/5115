"""Shared test fixtures: PostgreSQL container, migrations, app client, fakes, JWT factory.

PostgreSQL is real (testcontainers) per 06-testing-strategy.md. Anthropic and StoreKit are
mocked at the client boundary (AsyncAnthropic / StoreKitVerifier) keeping the contract.
Redis-backed rate limiting fails open when no Redis is present, so chat/other endpoints work
in tests without a Redis container; dedicated rate-limit behavior is exercised via the
limiter directly with a fake client.
"""

from __future__ import annotations

import base64
import datetime
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

# --- Environment must be set before app.config import (lru_cache settings) ---
# Hermetic test env: the suite MUST NOT depend on a root .env (devops keeps one for the
# docker-compose e2e bring-up, where JWT_ISSUER=claude-ios-e2e / JWT_AUDIENCE=claude-ios and
# a foreign JWT_PUBLIC_KEY would otherwise be loaded by pydantic-settings and 401 every
# authenticated request). Process env (os.environ[...] = ...) outranks the .env file in
# pydantic-settings, so we FORCE every auth/behaviour-defining variable here (not setdefault,
# which would leave a value the .env already injected). Per-test overrides still use
# monkeypatch.setenv on top of these.
_MASTER_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes base64
os.environ["KMS_LOCAL_MASTER_KEY"] = _MASTER_KEY_B64
os.environ["KMS_KEY_ID"] = ""
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-service-test"
# OPENAI_API_KEY is now BEHAVIOUR-DEFINING (ADR-058 §3 key-gate): image.generate is offered to the
# model IFF this key is non-empty after strip(). A root .env carries a REAL OpenAI key for the
# docker-compose e2e bring-up; loading it here would (a) leak a live key into the hermetic suite and
# (b) make the offered-set non-deterministic. FORCE a non-empty PLACEHOLDER (not the real key, never
# a network call — the image generator is faked at the singleton boundary, see FakeImageGenerator):
# non-empty keeps image.generate in the default offered set (the CI posture, which pins
# OPENAI_API_KEY=placeholder), and it is deterministic regardless of any .env. Per-test key-gate
# checks flip it via monkeypatch.setenv + get_settings.cache_clear().
os.environ["OPENAI_API_KEY"] = "sk-openai-service-test"
# LLM_PROVIDER must be pinned for the whole suite: the code default may change (e.g. to openai in a
# later sprint) and would then silently switch every existing test onto the OpenAI path. Force the
# anthropic posture explicitly (get_settings is lru_cached, so this must precede app.config import).
# Per-test OpenAI-path checks still override with monkeypatch.setenv("LLM_PROVIDER", "openai").
os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["APPSTORE_BUNDLE_ID"] = "com.example.app"
os.environ["APPSTORE_ENVIRONMENT"] = "sandbox"
os.environ["APPSTORE_ROOT_CERT_DIR"] = ""
# StoreKit must default to the prod posture (fail-closed real JWS). The .env e2e profile sets
# test-mode true + a secret; that would change StoreKit behaviour for tests that don't patch.
os.environ["STOREKIT_TEST_MODE"] = "false"
os.environ["STOREKIT_TEST_SECRET"] = ""
# API docs default true (test_api_documentation asserts get_settings().docs_enabled is True).
os.environ["DOCS_ENABLED"] = "true"
# Observability: tests expect an unprotected /metrics (200 without a token). The .env e2e
# profile sets METRICS_SCRAPE_TOKEN (=> /metrics 403 without header); force it empty.
os.environ["METRICS_SCRAPE_TOKEN"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# JWT: tokens are signed below with an ephemeral RSA key (_PRIVATE_PEM); the service must
# verify with the matching JWT_PUBLIC_KEY and the iss/aud baked into make_jwt(). Force a
# static public-key posture (no JWKS) and a fixed issuer/audience the factory mirrors.
_TEST_JWT_ISSUER = "claude-ios-tests"
_TEST_JWT_AUDIENCE = "claude-ios-tests"
os.environ["JWT_JWKS_URL"] = ""
os.environ["JWT_ISSUER"] = _TEST_JWT_ISSUER
os.environ["JWT_AUDIENCE"] = _TEST_JWT_AUDIENCE

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

# ----------------------------- RSA / JWT key material -----------------------------
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
).decode()
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
)

# Force (not setdefault): a root .env injects a foreign JWT_PUBLIC_KEY whose private half we
# do not hold, so tokens signed with _PRIVATE_PEM would fail signature verification.
os.environ["JWT_PUBLIC_KEY"] = _PUBLIC_PEM


def make_jwt(
    user_id: uuid.UUID | str,
    *,
    device_id: str | None = "dev-1",
    expired: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.datetime.now(tz=datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    # iss/aud MUST match the forced JWT_ISSUER/JWT_AUDIENCE so verify() (which checks both
    # when configured) accepts the token regardless of any root .env profile.
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "exp": exp,
        "iat": now,
        "iss": _TEST_JWT_ISSUER,
        "aud": _TEST_JWT_AUDIENCE,
    }
    if device_id is not None:
        claims["device_id"] = device_id
    if extra:
        claims.update(extra)
    return pyjwt.encode(claims, _PRIVATE_PEM, algorithm="RS256")


# ----------------------------- PostgreSQL container -----------------------------
@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        os.environ["DATABASE_URL"] = url
        yield url


@pytest.fixture(scope="session")
def _migrated(pg_url: str) -> Iterator[str]:
    """Run alembic migrations once against the container."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    yield pg_url


@pytest.fixture
async def _engine(_migrated: str):
    # Function-scoped engine so its asyncpg connections live on the same loop as the test
    # (pytest-asyncio runs each test on a fresh function-scoped loop). The container and
    # migrations remain session-scoped; only the connection pool is per-test.
    engine = create_async_engine(_migrated, future=True, poolclass=NullPool)
    yield engine
    await engine.dispose()


_TABLES = (
    "audit_logs",
    # ADR-058 (migration 0016): generated_images.user_id → users (CASCADE), session_id →
    # chat_sessions (SET NULL). Listed explicitly so RESTART IDENTITY/CASCADE resets it between
    # tests — otherwise image rows (which OUTLIVE their session, and persist even for a temporary
    # chat with session_id=NULL) would leak across tests and break count assertions.
    "generated_images",
    "tool_calls",
    "chat_steps",
    # Workspaces (ADR-036, migration 0011): workspace_files → workspace_projects (CASCADE) and
    # chat_sessions.workspace_project_id (SET NULL). Listed explicitly so RESTART IDENTITY/CASCADE
    # resets them deterministically between tests (and ordered before chat_sessions for clarity).
    "workspace_files",
    "workspace_projects",
    "chat_sessions",
    "byok_keys",
    # user_preferences must be truncated between tests so preferences-integration state does
    # not leak across tests (Figma-gap migration 0004 table; FK→users, but TRUNCATE CASCADE on
    # users would not reset RESTART IDENTITY for it unless listed explicitly).
    "user_preferences",
    "ledger_transactions",
    "wallets",
    "subscriptions",
    "users",
)


@pytest.fixture
async def db_sessionmaker(_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test clean DB: truncate all tables, yield a sessionmaker bound to the container."""
    maker = async_sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    async with _engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield maker


@pytest.fixture
async def db_session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as session:
        yield session


# ----------------------------- Fakes for external clients -----------------------------
class FakeAnthropicClient:
    """In-memory stand-in for AnthropicClient honoring the same LLMClient contract (ADR-033).

    Scriptable: `responses` is a list of LLMResult returned in order on create_message.
    validate_key returns `valid_keys` membership.

    ADR-033 boundary mirror: the orchestrator now passes the provider-NEUTRAL contract to
    ``create_message`` — ``messages`` is ``list[NeutralMessage]``, ``tools`` are neutral
    (dotted-name) definitions, and first-turn ``attachments`` arrive as a separate kwarg
    (PreparedAttachments). Provider wire serialization lives INSIDE the real client
    (``AnthropicClient._build_provider_messages`` / ``_serialize_tools`` / attachment injection).
    To keep this fake a faithful double of the real seam, ``create_message`` records BOTH:
    - the neutral kwargs (``messages``/``tools``/``attachments``/``api_key``/``system_prompt``)
      under ``neutral_*`` keys, AND
    - the Anthropic WIRE view the production client would actually send (``messages`` = wire
      dicts with ``content`` lists / ``tool_use`` / ``tool_result`` blocks; ``tools`` = underscore
      names; first-turn attachment content blocks injected into the last user wire message).
    The wire view is produced by reusing the REAL ``AnthropicClient`` translation helpers, so the
    recorded ``calls[*]`` reflect the exact wire contract — boundary tests assert on ``calls[*]``.
    """

    def __init__(self) -> None:
        from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

        self._AnthropicResult = AnthropicResult
        self._AnthropicUsage = AnthropicUsage
        self.responses: list[Any] = []
        # calls[*] hold the Anthropic WIRE view (see create_message) so boundary tests assert on
        # wire messages/tools exactly as the real AnthropicClient would send them. The original
        # neutral kwargs are preserved alongside (neutral_messages/neutral_tools/...).
        self.calls: list[dict[str, Any]] = []
        self.valid_keys: set[str] = set()
        # Keys for which validate_key must report KeyValidation.offline (network/non-401).
        self.offline_keys: set[str] = set()
        self.auth_error_keys: set[str] = set()
        self.raise_upstream = False

    def text_result(self, text: str = "hello") -> Any:
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return self._AnthropicResult(
            stop_reason="end_turn",
            content_blocks=[{"type": "text", "text": text}],
            usage=usage,
            text=text,
            tool_uses=[],
        )

    def parallel_tool_result(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        text: str = "",
        tool_ids: list[str] | None = None,
    ) -> Any:
        """ADR-025: one assistant turn with MULTIPLE tool_use blocks (parallel tool use).

        ``calls`` is an ordered list of (tool_name, args). content_blocks carry an optional
        leading text block then one tool_use block per call (in order); tool_uses mirrors them.
        Each tool_use.id is a realistic ``toolu_...`` (BUG-4 invariant), distinct per block.
        ``tool_ids`` (optional) pins the provider ids in order.
        """
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        tool_uses: list[dict[str, Any]] = []
        for i, (tool_name, args) in enumerate(calls):
            tid = (
                tool_ids[i]
                if tool_ids is not None and i < len(tool_ids)
                else f"toolu_{uuid.uuid4().hex[:24]}"
            )
            block = {"type": "tool_use", "id": tid, "name": tool_name, "input": args}
            content_blocks.append(block)
            tool_uses.append({"id": tid, "name": tool_name, "input": args})
        return self._AnthropicResult(
            stop_reason="tool_use",
            content_blocks=content_blocks,
            usage=usage,
            text=text,
            tool_uses=tool_uses,
        )

    def max_tokens_result(
        self,
        *,
        text: str = "",
        truncated_tool: tuple[str, dict[str, Any]] | None = None,
        tool_id: str | None = None,
        output_tokens: int = 16000,
    ) -> Any:
        """ADR-025: a turn TRUNCATED by the output-token limit (stop_reason="max_tokens").

        Mirrors production: content_blocks may carry a partial text block plus an INCOMPLETE
        tool_use block (e.g. files.write missing ``content``). The orchestrator must NOT execute
        nor surface these blocks; tool_uses is left empty (no executable tool_use on truncation —
        the orchestrator dispatches purely on stop_reason). ``output_tokens`` ≈ the max_tokens cap.
        """
        usage = self._AnthropicUsage(
            input_tokens=1240,
            output_tokens=output_tokens,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        if truncated_tool is not None:
            tname, partial_args = truncated_tool
            tid = tool_id or f"toolu_{uuid.uuid4().hex[:24]}"
            content_blocks.append(
                {"type": "tool_use", "id": tid, "name": tname, "input": partial_args}
            )
        return self._AnthropicResult(
            stop_reason="max_tokens",
            content_blocks=content_blocks,
            usage=usage,
            text=text,
            tool_uses=[],
        )

    def tool_result(self, tool_name: str, args: dict[str, Any], tool_id: str | None = None) -> Any:
        # ADR-008 / BUG-4: the raw Anthropic tool_use.id has the realistic "toolu_..." shape, NOT a
        # UUID. The previous UUID-like default masked BUG-4 (domain uuid4 leaking into
        # tool_result.tool_use_id). A toolu_-shaped default makes tests exercise the real semantics:
        # the raw provider id must round-trip unchanged into the continuation history.
        tid = tool_id or f"toolu_{uuid.uuid4().hex[:24]}"
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return self._AnthropicResult(
            stop_reason="tool_use",
            content_blocks=[{"type": "tool_use", "id": tid, "name": tool_name, "input": args}],
            usage=usage,
            text="",
            tool_uses=[{"id": tid, "name": tool_name, "input": args}],
        )

    async def create_message(self, **kwargs: Any) -> Any:
        """LLMClient.create_message double (ADR-033): record the WIRE view, return scripted result.

        Reuses the REAL ``AnthropicClient`` translation helpers (``_build_provider_messages`` /
        ``_serialize_tools``) so the recorded ``messages``/``tools`` are exactly the Anthropic wire
        form the production client would send. First-turn ``attachments`` (PreparedAttachments) are
        injected into the LAST user wire message the same way the real client does, so attachment
        boundary tests see the full image/document block on turn 0 and placeholders-only on replay.
        """
        from app.chat.anthropic_client import AnthropicAuthError, AnthropicClient
        from app.errors import UpstreamError

        neutral_messages = kwargs.get("messages", [])
        neutral_tools = kwargs.get("tools", [])
        attachments = kwargs.get("attachments")
        api_key = kwargs.get("api_key")
        system_prompt = kwargs.get("system_prompt")
        # ADR-034: record the session-fixed model passed by the orchestrator so model-proboros
        # tests can assert create_message(model=<session.model>) (None → client uses its default).
        model = kwargs.get("model")

        # Translate neutral → Anthropic wire exactly as the real client does (ADR-033 §3/§4).
        wire_messages = AnthropicClient._build_provider_messages(neutral_messages)
        wire_tools = AnthropicClient._serialize_tools(neutral_tools)
        if attachments is not None and getattr(attachments, "content_blocks", None):
            # Inject the full attachment blocks into the last user turn — production parity.
            for wm in reversed(wire_messages):
                if wm.get("role") == "user":
                    existing = wm.get("content")
                    base = existing if isinstance(existing, list) else []
                    wm["content"] = [*base, *attachments.content_blocks]
                    break

        self.calls.append(
            {
                # Wire view (what Anthropic would actually receive) — boundary tests use these.
                "messages": wire_messages,
                "tools": wire_tools,
                "system_prompt": system_prompt,
                "api_key": api_key,
                "attachments": attachments,
                # ADR-034: session-fixed model handed to the client (None → provider default).
                "model": model,
                # Neutral kwargs preserved for tests that want to inspect the seam directly.
                "neutral_messages": neutral_messages,
                "neutral_tools": neutral_tools,
            }
        )

        if self.raise_upstream:
            raise UpstreamError("anthropic upstream error")
        if api_key is not None and api_key in self.auth_error_keys:
            raise AnthropicAuthError("unauthorized")
        if not self.responses:
            return self.text_result()
        return self.responses.pop(0)

    async def validate_key(self, api_key: str) -> Any:
        # ADR-016: production BYOKService.set_key expects a KeyValidation enum
        # (valid|invalid|offline), NOT a bool. Membership in valid_keys → valid; membership in
        # offline_keys → offline (network/non-401); otherwise → invalid (401).
        from app.chat.anthropic_client import KeyValidation

        if api_key in self.offline_keys:
            return KeyValidation.offline
        if api_key in self.valid_keys:
            return KeyValidation.valid
        return KeyValidation.invalid


# A minimal but REAL 1x1 PNG (valid header/IHDR/IDAT/IEND) — the fixed bytes the fake returns.
# Real bytes (not a sentinel string) so integration tests can assert the fetched GET /v1/images
# body is byte-identical and carries the image/png content-type.
_FIXED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class FakeImageGenerator:
    """In-memory ``ImageGenerator`` double (ADR-058 §3): fixed PNG + degrade modes.

    Injected at the ``image_client`` singleton boundary (``_image_generator_singleton``) like the
    Anthropic fake — the DI factory (``deps.get_orchestrator`` → ``get_image_generator``) then
    hands THIS fake to ``GlobalToolHandlers``, so NO real ``AsyncOpenAI`` is built and NO network is
    hit (hermetic; the suite passes with placeholder keys). ``mode`` selects the outcome:
    - ``"ok"`` (default) → returns ``GeneratedImageData(_FIXED_PNG, media_type)``;
    - ``"content_policy"`` → raises ``ImageContentPolicyError`` (degrade code ``content_policy``);
    - ``"failure"`` → raises ``ImageGenerationError`` (degrade code ``image_generation_failed``).
    Records each call's prompt/size/quality so tests can assert the args reached the generator (the
    PROMPT is inspected only inside the test process — it is never persisted/logged by production).
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.media_type = "image/png"
        self.data = _FIXED_PNG
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, *, prompt: str, size: str | None = None, quality: str | None = None
    ) -> Any:
        from app.chat.image_client import (
            GeneratedImageData,
            ImageContentPolicyError,
            ImageGenerationError,
        )

        self.calls.append({"prompt": prompt, "size": size, "quality": quality})
        if self.mode == "content_policy":
            raise ImageContentPolicyError("image prompt was rejected by the provider")
        if self.mode == "failure":
            raise ImageGenerationError("image generation failed")
        return GeneratedImageData(data=self.data, media_type=self.media_type)


class FakeStoreKitVerifier:
    """Scriptable StoreKit verifier. raise=True simulates a forged transaction (→422)."""

    def __init__(self) -> None:
        self.next_transaction: Any = None
        self.raise_error = False

    def verify(self, signed_transaction: str) -> Any:
        from app.errors import ValidationFailedError

        if self.raise_error:
            raise ValidationFailedError("StoreKit JWS signature invalid")
        assert self.next_transaction is not None, "FakeStoreKitVerifier not scripted"
        return self.next_transaction


@pytest.fixture
def fake_anthropic() -> FakeAnthropicClient:
    return FakeAnthropicClient()


@pytest.fixture
def fake_storekit() -> FakeStoreKitVerifier:
    return FakeStoreKitVerifier()


@pytest.fixture
def fake_image_generator() -> FakeImageGenerator:
    return FakeImageGenerator()


# ----------------------------- App client with overrides -----------------------------
@pytest.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
    fake_image_generator: FakeImageGenerator,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with DB pointed at the container and external clients faked."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.byok import service as byok_service
    from app.chat import anthropic_client as anthropic_mod
    from app.chat import image_client as image_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    # Override the DB dependency to use the container sessionmaker.
    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Patch external-client singletons used by deps wiring.
    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    byok_service.AnthropicClient = type(fake_anthropic)  # type: ignore[misc]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]
    # ADR-058: patch the image-generator singleton so deps.get_orchestrator (→ get_image_generator)
    # hands the FAKE to GlobalToolHandlers — no real AsyncOpenAI / network. Save & restore so the
    # fake never leaks into a test that constructs its own generator.
    orig_image_singleton = image_mod._image_generator_singleton
    image_mod._image_generator_singleton = fake_image_generator  # type: ignore[assignment]

    # Rate limiting fails open without Redis; force it explicitly so tests are deterministic.
    async def _allow_chat(**_kwargs: Any) -> bool:
        return True

    async def _allow_other(**_kwargs: Any) -> bool:
        return True

    orig_chat = rate_limit.enforce_chat_limits
    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_chat_limits = _allow_chat  # type: ignore[assignment]
    rate_limit.enforce_other_limits = _allow_other  # type: ignore[assignment]
    # The routers imported the names at module load — patch there too.
    from app.api_gateway.routers import byok as byok_router
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import subscription as sub_router
    from app.api_gateway.routers import wallet as wallet_router

    chat_router.enforce_chat_limits = _allow_chat  # type: ignore[assignment]
    wallet_router.enforce_other_limits = _allow_other  # type: ignore[assignment]
    byok_router.enforce_other_limits = _allow_other  # type: ignore[assignment]
    sub_router.enforce_other_limits = _allow_other  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    chat_router.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    wallet_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    byok_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    sub_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    image_mod._image_generator_singleton = orig_image_singleton  # type: ignore[assignment]


# ----------------------------- DB seeding helpers -----------------------------
async def seed_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    trial_used: bool = False,
    subscription: str | None = None,
    expires_in_hours: float | None = 24,
    balance: int | None = None,
    byok_enabled: bool = False,
    byok_status: str | None = None,
) -> uuid.UUID:
    """Insert a user with optional subscription / wallet / byok rows. Commits."""
    uid = user_id or uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, trial_used) VALUES (:id, :tu)"),
        {"id": str(uid), "tu": trial_used},
    )
    if subscription is not None:
        expires = None
        if expires_in_hours is not None:
            expires = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
                hours=expires_in_hours
            )
        await session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at) "
                "VALUES (:uid, :st, 'pro', :exp)"
            ),
            {"uid": str(uid), "st": subscription, "exp": expires},
        )
    if balance is not None:
        await session.execute(
            text("INSERT INTO wallets (user_id, balance) VALUES (:uid, :bal)"),
            {"uid": str(uid), "bal": balance},
        )
    if byok_status is not None:
        from app.byok.kms import get_kms_client

        kms = get_kms_client()
        import os as _os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek = _os.urandom(32)
        nonce = _os.urandom(12)
        enc_key = AESGCM(dek).encrypt(nonce, b"sk-ant-user-key", None)
        enc_dek = kms.encrypt_dek(dek)
        await session.execute(
            text(
                "INSERT INTO byok_keys (user_id, encrypted_key, encrypted_dek, nonce, "
                "key_status, enabled) VALUES (:uid, :ek, :ed, :n, :ks, :en)"
            ),
            {
                "uid": str(uid),
                "ek": enc_key,
                "ed": enc_dek,
                "n": nonce,
                "ks": byok_status,
                "en": byok_enabled,
            },
        )
    await session.commit()
    return uid


def auth_headers(user_id: uuid.UUID | str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(user_id, **kwargs)}"}

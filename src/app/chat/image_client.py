"""Image generation client (ADR-058): OpenAI gpt-image-1 via a SEPARATE images.generate client.

Deliberately NOT ``LLMClient`` and NOT the built-in Responses ``ImageGeneration`` tool (ADR-058 §3):
we need control over the raw BYTES (to store them in ``generated_images``) and over the BILLING (a
separate idempotent debit) that the built-in tool does not give. The client owns its own
``AsyncOpenAI`` instance with image-specific timeout/retries (config ``IMAGE_TIMEOUT_SECONDS`` /
``IMAGE_MAX_RETRIES``), independent of the chat ``OpenAIClient``.

gpt-image-1 wire notes (ADR-058 §3 / ADR-059):
- ``response_format`` is REJECTED by the API for gpt-image-1 — the result is ALWAYS base64
  (``resp.data[0].b64_json`` → ``base64.b64decode``); the format is chosen via ``output_format``
  (``png`` | ``jpeg`` | ``webp``), which also determines the returned ``content_type``.
- a content-policy refusal (``openai.BadRequestError``) → ``ImageContentPolicyError`` (the tool
  degrades to a tool_result error, the turn survives); any other failure → ``ImageGenerationError``.

TLS verification is enabled by default by the SDK (httpx). The OpenAI key is NEVER logged (redaction
covers ``key``/``secret``); the prompt is NOT logged here (TD-035).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import openai

from app.config import get_settings

# MIME type per gpt-image-1 output_format (ADR-058 §1: image/png | image/jpeg | image/webp).
_OUTPUT_FORMAT_TO_MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class ImageGenerationError(Exception):
    """Image generation failed for a NON-policy reason (upstream/network/oversize/misconfig).

    The tool handler degrades this to a tool_result error (the turn survives, ADR-058 §3) rather
    than failing the whole /chat/run — a transient image-provider fault must not 502 the chat.
    """


class ImageContentPolicyError(ImageGenerationError):
    """OpenAI refused the prompt on content policy (``openai.BadRequestError``, ADR-058 §3).

    Subclass of ``ImageGenerationError`` so any handler that catches the base also catches this;
    the handler surfaces a distinct ``content_policy`` tool-result code so the model can adjust the
    prompt within the same turn (graceful degrade, like ``time.now``'s ``invalid_timezone``).
    """


@dataclass(frozen=True)
class GeneratedImageData:
    """Raw bytes + MIME of one generated image (never base64, never the prompt)."""

    data: bytes
    media_type: str


@runtime_checkable
class ImageGenerator(Protocol):
    """Provider-agnostic image generation contract (ADR-058 §3).

    ``size``/``quality`` are ``None`` when the caller wants the instance default (resolved by the
    implementation from config). The implementation raises ``ImageContentPolicyError`` on a policy
    refusal and ``ImageGenerationError`` on any other failure — never returns an empty result.
    """

    async def generate(
        self, *, prompt: str, size: str | None = None, quality: str | None = None
    ) -> GeneratedImageData: ...


class OpenAIImageGenerator:
    """``ImageGenerator`` backed by OpenAI ``images.generate`` (gpt-image-1, ADR-058 §3).

    Owns its own ``AsyncOpenAI`` with image-specific timeout/retries (config), separate from the
    chat client. The service key is read from config; TLS verification stays on (SDK default).
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.image_model
        self._max_bytes = settings.image_max_bytes
        self._client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key or "placeholder",
            timeout=settings.image_timeout_seconds,
            max_retries=settings.image_max_retries,
        )

    async def generate(
        self, *, prompt: str, size: str | None = None, quality: str | None = None
    ) -> GeneratedImageData:
        settings = get_settings()
        # Resolve size/quality/output_format: a caller value (already validated by the tool-args
        # schema) wins; otherwise the graceful instance default (malformed env → safe default).
        resolved_size = size or settings.resolved_image_size()
        resolved_quality = quality or settings.resolved_image_quality()
        output_format = settings.resolved_image_output_format()
        try:
            # gpt-image-1: NEVER pass response_format (the API rejects it) — the result is always
            # base64 in ``data[0].b64_json``; ``output_format`` selects png/jpeg/webp (ADR-058 §3).
            # The SDK types size/quality/output_format as closed Literals; the values are already
            # validated (tool-args schema + resolved_* allowlists), so cast to satisfy the overload.
            resp = await self._client.images.generate(
                model=self._model,
                prompt=prompt,
                n=1,
                size=cast(Any, resolved_size),
                quality=cast(Any, resolved_quality),
                output_format=cast(Any, output_format),
            )
        except openai.BadRequestError as exc:
            # gpt-image-1 returns THREE distinct refusals ALL as BadRequestError (verified against
            # the LIVE API): a content-policy refusal (exc.code == "moderation_blocked"), an invalid
            # param value (exc.code == "invalid_value", e.g. a bad size), and an unknown param
            # (exc.code == "unknown_parameter"). ONLY moderation is a content-policy refusal the
            # model can fix by rewriting the prompt → ImageContentPolicyError (degrade quietly,
            # INFO). Every other 400 is a request error the model CANNOT fix by rewriting → treat as
            # a generation failure (WARNING + metric, code image_generation_failed) so it is visible
            # in monitoring instead of burning tool-rounds into a 502.
            #
            # Discriminate on ``exc.code`` ONLY: ``exc.type`` is IDENTICAL for moderation and
            # invalid_value (``image_generation_user_error``), and ``exc.param`` being None for
            # moderation is a coincidence, not a contract. A missing/None code →
            # ImageGenerationError (safer to flag an infra fault than to silently ask the model to
            # rewrite the prompt). The message is FIXED (never ``str(exc)`` — the provider text may
            # echo prompt fragments, TD-035).
            if getattr(exc, "code", None) == "moderation_blocked":
                raise ImageContentPolicyError(
                    "image prompt was rejected by the content policy"
                ) from exc
            raise ImageGenerationError("image generation request was rejected") from exc
        except openai.OpenAIError as exc:
            # Any other upstream/network failure. The prompt/key are never logged here.
            raise ImageGenerationError("image generation failed") from exc

        data_list = resp.data or []
        if not data_list or not getattr(data_list[0], "b64_json", None):
            raise ImageGenerationError("image generation returned no data")
        b64 = data_list[0].b64_json
        assert b64 is not None  # noqa: S101 - guarded above
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageGenerationError("image generation returned invalid base64") from exc
        if len(raw) > self._max_bytes:
            raise ImageGenerationError("generated image exceeds the byte limit")
        return GeneratedImageData(
            data=raw, media_type=_OUTPUT_FORMAT_TO_MIME.get(output_format, "image/png")
        )


_image_generator_singleton: OpenAIImageGenerator | None = None


def get_image_generator() -> ImageGenerator:
    """Process-wide ``ImageGenerator`` singleton (ADR-058, mirrors ``_get_openai_singleton``).

    Lazily constructed once per process. The constructor reads only config (``OPENAI_API_KEY`` /
    ``IMAGE_*``); it does not depend on ``LLM_PROVIDER``. On a non-OpenAI instance with no OpenAI
    key an ``image.generate`` call degrades to a tool_result error (the turn survives) rather than
    crashing — the image tool is only useful on OpenAI instances (ADR-058 §3).
    """
    global _image_generator_singleton
    if _image_generator_singleton is None:
        _image_generator_singleton = OpenAIImageGenerator()
    return _image_generator_singleton

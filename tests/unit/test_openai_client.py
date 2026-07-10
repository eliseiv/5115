"""Unit tests for the OpenAI LLMClient implementation and the provider factory (ADR-033/ADR-059).

No real OpenAI calls: the SDK async client (``responses.create`` / ``models.list``) is replaced with
an in-memory fake, and the response is built from the REAL pinned SDK output types
(``ResponseOutputMessage`` / ``ResponseFunctionToolCall`` / ``ResponseFunctionWebSearch``) so the
``isinstance`` seam and ``model_dump`` persistence match production. Covers the Responses-API seam
(ADR-059): status/incomplete_details→canonical stop_reason, function_call→domain tool_uses
(reverse-map + JSON arg parse + invalid-JSON/unknown-name rejection, raw ``call_id`` round-trip —
ADR-008), ``web_search_call`` never surfaced as a tool_use, flat function-tool wire shape +
server-side gating, ``instructions``/``max_output_tokens``/``store`` wiring, attachment injection
(input_image / native input_file PDF — ADR-059 §6), usage (cached_tokens→cache_read), validate_key
outcomes, upstream-error mapping, and the ``get_llm_client()`` factory dispatch by ``LLM_PROVIDER``.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_function_web_search import ResponseFunctionWebSearch

from app.chat.attachments import PreparedAttachments, prepare_attachments
from app.chat.llm_client import (
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    GenerationOptions,
    KeyValidation,
    NeutralMessage,
    get_llm_client,
)
from app.chat.openai_client import OpenAIAuthError, OpenAIClient
from app.chat.tools import neutral_tool_definitions
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.schemas.chat import AttachmentIn

# --------------------------------------------------------------------------------------------
# Builders for the Responses ``output[]`` items (real pinned-SDK types — the client isinstance-
# checks ResponseOutputMessage / ResponseFunctionToolCall and calls item.model_dump()).
# --------------------------------------------------------------------------------------------


def _text_message(text: str = "hi", *, id: str = "msg_1") -> ResponseOutputMessage:
    part = ResponseOutputText(text=text, type="output_text", annotations=[])
    return ResponseOutputMessage(
        id=id, content=[part], role="assistant", status="completed", type="message"
    )


def _function_call(
    *,
    id: str = "fc_1",
    call_id: str = "call_1",
    name: str = "files_read",
    arguments: str = "{}",
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=id,
        call_id=call_id,
        name=name,
        arguments=arguments,
        type="function_call",
        status="completed",
    )


def _web_search_call(*, id: str = "ws_1") -> ResponseFunctionWebSearch:
    return ResponseFunctionWebSearch(
        id=id, type="web_search_call", status="completed", action={"type": "search", "query": "x"}
    )


def _usage(
    *, input_tokens: int = 100, output_tokens: int = 20, cached: int | None = None
) -> SimpleNamespace:
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, input_tokens_details=details
    )


_USAGE_DEFAULT = object()  # sentinel: distinguishes "omitted" from an explicit usage=None.


def _response(
    *,
    output: list[Any] | None = None,
    status: str = "completed",
    incomplete_details: Any | None = None,
    usage: Any | None = _USAGE_DEFAULT,
) -> SimpleNamespace:
    """A stand-in for the SDK ``Response`` (reads output/status/incomplete_details/usage).

    ``usage`` defaults to a populated usage object; pass ``usage=None`` explicitly to exercise the
    "no usage" branch (the sentinel keeps that distinct from the default).
    """
    return SimpleNamespace(
        output=output if output is not None else [_text_message()],
        status=status,
        incomplete_details=incomplete_details,
        usage=_usage() if usage is _USAGE_DEFAULT else usage,
    )


# --------------------------------------------------------------------------------------------
# Fakes for the OpenAI SDK async client. The client under test only touches
# ``responses.create(...)`` / ``models.list()`` / ``with_options(api_key=...)``.
# --------------------------------------------------------------------------------------------


class _FakeResponses:
    def __init__(self) -> None:
        self.next_response: Any = None
        self.raise_exc: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.next_response


class _FakeModels:
    def __init__(self) -> None:
        self.raise_exc: Exception | None = None
        self.called = False

    async def list(self) -> Any:
        self.called = True
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(data=[])


class _FakeAsyncOpenAI:
    """Stand-in for openai.AsyncOpenAI: exposes responses and models, plus with_options."""

    def __init__(self) -> None:
        self.responses = _FakeResponses()
        self.models = _FakeModels()
        self.options_key: str | None = None
        self.options_timeout: Any = None

    def with_options(self, **kwargs: Any) -> _FakeAsyncOpenAI:
        # The real SDK returns a copied client composing options; the fake mutates & returns self.
        # api_key and timeout compose independently (deep_thinking BYOK sets BOTH — the api_key set
        # by an earlier with_options(api_key=...) must NOT be dropped by a later timeout override).
        if "api_key" in kwargs:
            self.options_key = kwargs["api_key"]
        if "timeout" in kwargs:
            self.options_timeout = kwargs["timeout"]
        return self


def _client_with_fake() -> tuple[OpenAIClient, _FakeAsyncOpenAI]:
    client = OpenAIClient()
    fake = _FakeAsyncOpenAI()
    client._client = fake  # type: ignore[assignment]
    return client, fake


def _req() -> httpx.Request:
    return httpx.Request("GET", "https://api.openai.com/v1/x")


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "unauthorized", response=httpx.Response(401, request=_req()), body=None
    )


# ============================ status/details → canonical stop_reason ============================
@pytest.mark.asyncio
async def test_function_call_item_maps_to_tool_use_stop_reason() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(output=[_function_call()], status="completed")
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_TOOL_USE


@pytest.mark.asyncio
async def test_incomplete_max_output_tokens_maps_to_max_tokens() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_text_message()],
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_MAX_TOKENS


@pytest.mark.asyncio
async def test_incomplete_details_none_does_not_raise_and_is_end_turn() -> None:
    # incomplete_details can be None even when status=='incomplete' — must not AttributeError.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_text_message()], status="incomplete", incomplete_details=None
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_END_TURN


@pytest.mark.asyncio
async def test_incomplete_other_reason_is_end_turn() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_text_message()],
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="content_filter"),
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_END_TURN


@pytest.mark.asyncio
async def test_completed_message_maps_to_end_turn_and_text() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(output=[_text_message("hello there")])
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_END_TURN
    assert result.text == "hello there"


# ============================ function_call → domain tool_uses ============================
@pytest.mark.asyncio
async def test_function_call_reverse_mapped_and_arguments_parsed() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[
            _function_call(
                id="fc_a", call_id="call_a", name="files_read", arguments='{"path": "a.txt"}'
            ),
            _function_call(
                id="fc_b",
                call_id="call_b",
                name="calendar_read",
                arguments='{"start": "x", "end": "y"}',
            ),
        ],
        status="completed",
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_TOOL_USE
    # underscore wire name → dotted domain name; arguments JSON-string → dict; raw call_id kept.
    assert result.tool_uses == [
        {"id": "call_a", "name": "files.read", "input": {"path": "a.txt"}},
        {"id": "call_b", "name": "calendar.read", "input": {"start": "x", "end": "y"}},
    ]


@pytest.mark.asyncio
async def test_tool_use_id_is_call_id_not_fc_id() -> None:
    # ADR-008/ADR-059 §1: correlate on call_id (call_...), NOT the item id (fc_...). Round-trip.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_function_call(id="fc_x", call_id="call_x", name="files_read", arguments="{}")],
        status="completed",
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.tool_uses[0]["id"] == "call_x"
    assert result.tool_uses[0]["id"] != "fc_x"


@pytest.mark.asyncio
async def test_content_blocks_persist_output_items_verbatim() -> None:
    # content_blocks = [item.model_dump(mode="json", exclude_none=True) for item in output].
    client, fake = _client_with_fake()
    msg = _text_message("hi", id="msg_1")
    fc = _function_call(id="fc_1", call_id="call_1", name="files_read", arguments="{}")
    fake.responses.next_response = _response(output=[msg, fc], status="completed")
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.content_blocks == [
        msg.model_dump(mode="json", exclude_none=True),
        fc.model_dump(mode="json", exclude_none=True),
    ]


@pytest.mark.asyncio
async def test_function_call_invalid_json_arguments_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_function_call(call_id="call_x", name="files_read", arguments="{not json")],
        status="completed",
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_function_call_non_object_arguments_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_function_call(call_id="call_x", name="files_read", arguments="[1, 2, 3]")],
        status="completed",
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_function_call_unknown_name_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_function_call(call_id="call_x", name="totally_unknown_tool", arguments="{}")],
        status="completed",
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_function_call_empty_arguments_string_becomes_empty_dict() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_function_call(call_id="call_x", name="files_list", arguments="")],
        status="completed",
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.tool_uses == [{"id": "call_x", "name": "files.list", "input": {}}]


# ============================ web_search_call is never a tool_use ============================
@pytest.mark.asyncio
async def test_web_search_call_not_surfaced_as_tool_use() -> None:
    # ADR-059 §1: web_search_call is persisted but NEVER a tool_use and never reverse-mapped
    # (its name would not be in the static map — must not raise UnknownToolNameError).
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_web_search_call(), _text_message("done")], status="completed"
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.tool_uses == []
    assert result.stop_reason == STOP_REASON_END_TURN
    # still persisted verbatim in content_blocks (both items).
    assert len(result.content_blocks) == 2
    assert any(b["type"] == "web_search_call" for b in result.content_blocks)


@pytest.mark.asyncio
async def test_web_search_call_alongside_function_call_only_function_becomes_tool_use() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[
            _web_search_call(),
            _function_call(call_id="call_z", name="files_read", arguments="{}"),
        ],
        status="completed",
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_TOOL_USE
    assert result.tool_uses == [{"id": "call_z", "name": "files.read", "input": {}}]


# ============================ input building from neutral history ============================
@pytest.mark.asyncio
async def test_system_prompt_passed_via_instructions_not_input() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    messages = [NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "hi"}])]
    await client.create_message(
        system_prompt="SYSTEM", messages=messages, tools=[], attachments=None
    )
    call = fake.responses.calls[0]
    assert call["instructions"] == "SYSTEM"
    # system is NOT injected as a first input item — the first input item is the user message.
    assert call["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    # no input item is a system/instructions message.
    assert all(item.get("role") != "system" for item in call["input"])


@pytest.mark.asyncio
async def test_build_input_replays_assistant_output_and_tool_result_by_call_id() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    assistant_block = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_x",
        "name": "files_read",
        "arguments": "{}",
        "status": "completed",
    }
    messages = [
        NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "hi"}]),
        NeutralMessage(role="assistant", content_blocks=[assistant_block]),
        NeutralMessage(
            role="tool",
            tool_call_id="dom-1",
            provider_tool_use_id="call_x",
            tool_name="files.read",
            result={"ok": True},
        ),
    ]
    await client.create_message(system_prompt="s", messages=messages, tools=[], attachments=None)
    sent = fake.responses.calls[0]["input"]
    # user message → Responses message item with input_text parts.
    assert sent[0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    # assistant output[] replayed VERBATIM (accepted back as input).
    assert sent[1] == assistant_block
    # tool result → function_call_output keyed by the raw call_id, JSON-encoded output.
    assert sent[2] == {
        "type": "function_call_output",
        "call_id": "call_x",
        "output": json.dumps({"ok": True}),
    }


@pytest.mark.asyncio
async def test_tool_role_error_serialized_into_function_call_output() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    messages = [
        NeutralMessage(
            role="tool",
            provider_tool_use_id="call_1",
            error={"message": "boom", "code": "x"},
        ),
    ]
    await client.create_message(system_prompt="s", messages=messages, tools=[], attachments=None)
    item = fake.responses.calls[0]["input"][0]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call_1"
    assert json.loads(item["output"]) == {"message": "boom", "code": "x"}


@pytest.mark.asyncio
async def test_raw_dict_message_passed_through_unchanged() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    raw = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "raw"}]}
    await client.create_message(system_prompt="s", messages=[raw], tools=[], attachments=None)
    assert fake.responses.calls[0]["input"][0] == raw


# ============================ tools (flat Responses wire) + gating ============================
@pytest.mark.asyncio
async def test_serialize_tools_to_flat_function_shape() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=True),
        attachments=None,
    )
    sent_tools = fake.responses.calls[0]["tools"]
    for t in sent_tools:
        # FLAT shape: name/parameters/strict at the TOP level, NO nested ``function`` wrapper.
        assert t["type"] == "function"
        assert "function" not in t
        assert set(t) == {"type", "name", "description", "parameters", "strict"}
        assert "." not in t["name"]  # underscore transport name, dots forbidden
    names = {t["name"] for t in sent_tools}
    assert "files_read" in names
    assert "site_write_file" in names  # server-side offered when include_server_side=True
    assert "time_now" in names


@pytest.mark.asyncio
async def test_server_side_gating_excludes_site_tools_when_no_project() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False),
        attachments=None,
    )
    names = {t["name"] for t in fake.responses.calls[0]["tools"]}
    assert not any(n.startswith("site_") for n in names)  # site.* excluded
    assert "files_read" in names  # client-side still offered
    assert "time_now" in names  # global server-side always offered (ADR-026)


@pytest.mark.asyncio
async def test_client_side_gating_excludes_client_tools_when_temporary() -> None:
    # ADR-056: a temporary chat drops client-side tools (files.*/calendar.*/reminders.*) — they
    # need DB continuation via /chat/tool-result, unavailable without persistence. Server-side
    # tools (site.* with a project + the global time.now) stay offered — executed in-request.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=True, include_client_side=False),
        attachments=None,
    )
    names = {t["name"] for t in fake.responses.calls[0]["tools"]}
    # No client-side function-tools are offered.
    assert not any(n.startswith("files_") for n in names)
    assert not any(n.startswith("calendar_") for n in names)
    assert not any(n.startswith("reminders_") for n in names)
    # Server-side tools remain: project-scoped site.* (include_server_side=True) + global time.now.
    assert "site_write_file" in names
    assert "time_now" in names


@pytest.mark.asyncio
async def test_client_side_gating_temporary_no_project_leaves_global_server_side_tools() -> None:
    # ADR-056 + ADR-022: temporary («чистый чат», no project) → client-side dropped AND site.*
    # dropped; only the always-offered GLOBAL server-side tools survive. ADR-058: image.generate is
    # global and its key-gate is satisfied (conftest forces a non-empty OPENAI_API_KEY), so the
    # surviving set is {time_now, image_generate} (quiz_generate stays dialog-gated to study_learn).
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False, include_client_side=False),
        attachments=None,
    )
    names = {t["name"] for t in fake.responses.calls[0]["tools"]}
    assert names == {"time_now", "image_generate"}


@pytest.mark.asyncio
async def test_no_tools_passes_omit() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert fake.responses.calls[0]["tools"] is openai.omit


# ================== instructions / max_output_tokens / store / model ==================
@pytest.mark.asyncio
async def test_max_output_tokens_and_model_passed() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    call = fake.responses.calls[0]
    assert call["max_output_tokens"] == get_settings().openai_max_tokens
    assert call["model"] == get_settings().openai_model


@pytest.mark.asyncio
async def test_store_defaults_to_omit() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert fake.responses.calls[0]["store"] is openai.omit


@pytest.mark.asyncio
async def test_store_false_when_temporary_option() -> None:
    # ADR-056/ADR-059: a temporary chat opts out of provider persistence via store=False.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        options=GenerationOptions(temporary=True),
    )
    assert fake.responses.calls[0]["store"] is False


@pytest.mark.asyncio
async def test_store_omit_when_option_not_temporary() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        options=GenerationOptions(temporary=False),
    )
    assert fake.responses.calls[0]["store"] is openai.omit


# ============================ usage parsing ============================
@pytest.mark.asyncio
async def test_usage_cached_tokens_map_to_cache_read_write_zero() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        usage=_usage(input_tokens=300, output_tokens=40, cached=128)
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 40
    assert result.usage.cache_read_tokens == 128
    assert result.usage.cache_write_tokens == 0  # OpenAI has no explicit write count
    assert result.usage.model == get_settings().openai_model


@pytest.mark.asyncio
async def test_usage_without_details_defaults_cache_read_zero() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(usage=_usage(cached=None))
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0


@pytest.mark.asyncio
async def test_usage_absent_yields_zeros() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(usage=None)
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.cache_read_tokens == 0


# ============ attachments: prepare mapping (Responses parts, ADR-059 §6) ============
def _png_b64() -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")


def test_openai_attachment_image_maps_to_input_image_data_uri() -> None:
    prepared = prepare_attachments(
        [AttachmentIn(type="image", mediaType="image/png", filename="p.png", data=_png_b64())],
        get_settings(),
        provider="openai",
    )
    block = prepared.content_blocks[0]
    assert block["type"] == "input_image"
    assert block["image_url"].startswith("data:image/png;base64,")  # plain string, not {url:...}
    assert block["detail"] == "auto"


def test_openai_attachment_text_maps_to_input_text_block() -> None:
    data = base64.b64encode(b"hello world").decode("ascii")
    prepared = prepare_attachments(
        [AttachmentIn(type="text", mediaType="text/plain", filename="n.txt", data=data)],
        get_settings(),
        provider="openai",
    )
    block = prepared.content_blocks[0]
    assert block["type"] == "input_text"
    assert "hello world" in block["text"]


def _pdf_b64(pages: int = 1, *, encrypt: str | None = None) -> str:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypt is not None:
        writer.encrypt(encrypt)
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_openai_attachment_pdf_maps_to_native_input_file() -> None:
    # ADR-059 §6 (revises ADR-041 transport): PDF on OpenAI → native Responses input_file part.
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="doc.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="openai")
    assert len(prepared.content_blocks) == 1
    block = prepared.content_blocks[0]
    assert block["type"] == "input_file"
    assert block["filename"] == "doc.pdf"
    assert block["file_data"] == f"data:application/pdf;base64,{pdf_b64}"


def test_openai_attachment_pdf_default_filename_when_none() -> None:
    att = AttachmentIn(type="document", mediaType="application/pdf", filename=None, data=_pdf_b64())
    block = prepare_attachments([att], get_settings(), provider="openai").content_blocks[0]
    assert block["filename"] == "file"


def test_openai_attachment_pdf_storage_invariant_placeholder_no_base64() -> None:
    # ADR-020 §3 / ADR-041 §5: raw base64 lives ONLY in the in-memory content block.
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="secret.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="openai")
    assert len(prepared.placeholders) == 1
    ph = prepared.placeholders[0]
    assert ph["type"] == "text"
    assert pdf_b64 not in ph["text"]  # no raw base64
    assert "file_data" not in ph["text"]  # no data-URI key leaked
    assert "application/pdf" in ph["text"]  # human-readable metadata only
    assert "secret.pdf" in ph["text"]


# --- shared validation still applies BEFORE the provider branch (still 422) ---
def test_openai_attachment_encrypted_pdf_still_422() -> None:
    att = AttachmentIn(
        type="document", mediaType="application/pdf", data=_pdf_b64(encrypt="secret")
    )
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], get_settings(), provider="openai")


def test_openai_attachment_corrupt_pdf_still_422() -> None:
    bad = base64.b64encode(b"%PDF-1.4\nnot a real pdf body\n%%EOF").decode("ascii")
    att = AttachmentIn(type="document", mediaType="application/pdf", data=bad)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], get_settings(), provider="openai")


# --- Anthropic PDF mapping is UNCHANGED (native document block), regression ---
def test_anthropic_attachment_pdf_unchanged_native_document_block() -> None:
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="doc.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="anthropic")
    assert prepared.content_blocks[0] == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
    }


# ============== attachments: injection into last user item ==============
@pytest.mark.asyncio
async def test_image_input_image_injected_into_last_user_item() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    prepared = PreparedAttachments(
        content_blocks=[
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"}
        ],
        placeholders=[],
    )
    messages = [NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "look"}])]
    await client.create_message(
        system_prompt="s", messages=messages, tools=[], attachments=prepared
    )
    user_item = fake.responses.calls[0]["input"][-1]
    assert user_item["type"] == "message"
    assert user_item["role"] == "user"
    assert isinstance(user_item["content"], list)
    assert {"type": "input_text", "text": "look"} in user_item["content"]
    assert any(p.get("type") == "input_image" for p in user_item["content"])


@pytest.mark.asyncio
async def test_pdf_input_file_injected_into_last_user_item() -> None:
    # ADR-059 §6: the native input_file part is injected into the last user item like image/text.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    prepared = PreparedAttachments(
        content_blocks=[
            {
                "type": "input_file",
                "filename": "doc.pdf",
                "file_data": "data:application/pdf;base64,JVBERi0=",
            }
        ],
        placeholders=[],
    )
    messages = [NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "read"}])]
    await client.create_message(
        system_prompt="s", messages=messages, tools=[], attachments=prepared
    )
    user_item = fake.responses.calls[0]["input"][-1]
    assert user_item["role"] == "user"
    assert isinstance(user_item["content"], list)
    assert any(p.get("type") == "input_file" for p in user_item["content"])


# ============================ validate_key ============================
@pytest.mark.asyncio
async def test_validate_key_ok_returns_valid() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = None
    assert await client.validate_key("sk-openai-good") is KeyValidation.valid
    assert fake.options_key == "sk-openai-good"
    assert fake.models.called


@pytest.mark.asyncio
async def test_validate_key_401_returns_invalid() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = _auth_error()
    assert await client.validate_key("sk-bad") is KeyValidation.invalid


@pytest.mark.asyncio
async def test_validate_key_timeout_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APITimeoutError(request=_req())
    assert await client.validate_key("sk-x") is KeyValidation.offline


@pytest.mark.asyncio
async def test_validate_key_connection_error_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APIConnectionError(message="conn", request=_req())
    assert await client.validate_key("sk-x") is KeyValidation.offline


@pytest.mark.asyncio
async def test_validate_key_non_401_status_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APIStatusError(
        "boom", response=httpx.Response(500, request=_req()), body=None
    )
    assert await client.validate_key("sk-x") is KeyValidation.offline


# ============================ upstream error mapping in create_message ============================
@pytest.mark.asyncio
async def test_create_message_auth_error_raises_openai_auth_error() -> None:
    client, fake = _client_with_fake()
    fake.responses.raise_exc = _auth_error()
    with pytest.raises(OpenAIAuthError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_create_message_timeout_raises_upstream_error() -> None:
    client, fake = _client_with_fake()
    fake.responses.raise_exc = openai.APITimeoutError(request=_req())
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_create_message_connection_error_raises_upstream_error() -> None:
    client, fake = _client_with_fake()
    fake.responses.raise_exc = openai.APIConnectionError(message="conn", request=_req())
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_create_message_status_error_raises_upstream_error() -> None:
    client, fake = _client_with_fake()
    fake.responses.raise_exc = openai.APIStatusError(
        "boom", response=httpx.Response(500, request=_req()), body=None
    )
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_byok_api_key_override_applied() -> None:
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s", messages=[], tools=[], attachments=None, api_key="sk-byok"
    )
    assert fake.options_key == "sk-byok"


# ==================== dialog modes (ADR-055 §5 / ADR-059 §2,§3) ====================
@pytest.mark.asyncio
async def test_deep_thinking_sends_reasoning_store_false_and_include() -> None:
    # ADR-059 §3: deep_thinking → reasoning={"effort": ...} + store=False + the encrypted-reasoning
    # replay flag (include=["reasoning.encrypted_content"]) so the tool-loop continuation is valid.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        options=GenerationOptions(dialog_mode="deep_thinking"),
    )
    call = fake.responses.calls[0]
    assert call["reasoning"] == {"effort": get_settings().resolved_deep_thinking_effort()}
    assert call["store"] is False
    assert call["include"] == ["reasoning.encrypted_content"]


@pytest.mark.asyncio
async def test_deep_thinking_applies_per_call_timeout() -> None:
    # ADR-055 §5 / ADR-059 §3: deep_thinking gets its own longer timeout via with_options(timeout=).
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        options=GenerationOptions(dialog_mode="deep_thinking"),
    )
    assert fake.options_timeout == get_settings().deep_thinking_timeout_seconds


@pytest.mark.asyncio
async def test_deep_thinking_with_byok_composes_api_key_and_timeout() -> None:
    # deep_thinking + BYOK: with_options(api_key=...) AND with_options(timeout=...) compose — the
    # api_key must NOT be dropped by the later timeout override (ADR-044 BYOK + ADR-059 §3).
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        api_key="sk-byok",
        options=GenerationOptions(dialog_mode="deep_thinking"),
    )
    assert fake.options_key == "sk-byok"  # api_key preserved
    assert fake.options_timeout == get_settings().deep_thinking_timeout_seconds  # timeout composed


@pytest.mark.asyncio
async def test_search_adds_web_search_tool_alongside_function_tools() -> None:
    # ADR-059 §2: search → the GA web_search tool is added RIGHT NEXT TO our function-tools.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False),
        attachments=None,
        options=GenerationOptions(dialog_mode="search"),
    )
    sent_tools = fake.responses.calls[0]["tools"]
    web_search = [t for t in sent_tools if t.get("type") == "web_search"]
    assert len(web_search) == 1
    assert web_search[0]["search_context_size"] == get_settings().resolved_search_context_size()
    # function-tools are STILL present alongside the built-in web_search tool.
    assert any(t.get("type") == "function" for t in sent_tools)
    assert any(t.get("name") == "files_read" for t in sent_tools)


@pytest.mark.asyncio
async def test_search_web_search_call_output_not_surfaced_as_tool_use() -> None:
    # ADR-059 §1: even in search mode a web_search_call item never becomes a client tool_use and
    # never trips UnknownToolNameError (regression guard for the search branch specifically).
    client, fake = _client_with_fake()
    fake.responses.next_response = _response(
        output=[_web_search_call(), _text_message("cited answer")], status="completed"
    )
    result = await client.create_message(
        system_prompt="s",
        messages=[],
        tools=[],
        attachments=None,
        options=GenerationOptions(dialog_mode="search"),
    )
    assert result.tool_uses == []
    assert result.stop_reason == STOP_REASON_END_TURN


@pytest.mark.asyncio
async def test_study_learn_sends_no_reasoning_and_no_web_search() -> None:
    # ADR-055 §5: study_learn is prompt-only this sprint — NO reasoning param, NO web_search tool,
    # store stays at the API default (omit). The learning suffix lives in the orchestrator prompt.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False),
        attachments=None,
        options=GenerationOptions(dialog_mode="study_learn"),
    )
    call = fake.responses.calls[0]
    assert call["reasoning"] is openai.omit
    assert call["include"] is openai.omit
    assert call["store"] is openai.omit
    assert not any(t.get("type") == "web_search" for t in call["tools"])


@pytest.mark.asyncio
@pytest.mark.parametrize("dialog_mode", ["smart", None])
async def test_smart_and_none_are_sprint1_regression(dialog_mode: str | None) -> None:
    # smart / None → the sprint-1 posture: no reasoning, no include, no web_search, store=omit.
    client, fake = _client_with_fake()
    fake.responses.next_response = _response()
    options = GenerationOptions(dialog_mode=dialog_mode) if dialog_mode is not None else None
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False),
        attachments=None,
        options=options,
    )
    call = fake.responses.calls[0]
    assert call["reasoning"] is openai.omit
    assert call["include"] is openai.omit
    assert call["store"] is openai.omit
    assert not any(t.get("type") == "web_search" for t in call["tools"])


# ============================ factory get_llm_client() ============================
def test_factory_default_returns_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.anthropic_client as anthropic_mod
    import app.chat.llm_client as llm_mod
    from app.chat.anthropic_client import AnthropicClient

    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", None)
    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    get_settings.cache_clear()
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    try:
        client = get_llm_client()
        assert isinstance(client, AnthropicClient)
    finally:
        get_settings.cache_clear()


def test_factory_anthropic_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.anthropic_client as anthropic_mod
    import app.chat.llm_client as llm_mod
    from app.chat.anthropic_client import AnthropicClient

    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", None)
    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    try:
        assert isinstance(get_llm_client(), AnthropicClient)
    finally:
        get_settings.cache_clear()


def test_factory_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    try:
        client = get_llm_client()
        assert isinstance(client, OpenAIClient)
        # singleton: a second call returns the same instance.
        assert get_llm_client() is client
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(llm_mod, "_openai_singleton", None, raising=False)

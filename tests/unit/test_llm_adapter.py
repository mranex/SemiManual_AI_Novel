"""Kiểm tra LLM adapter, phân loại lỗi và structured output (T11).

Toàn bộ test offline: transport giả được inject vào `OpenAICompatibleClient`, và
`FakeLLMClient` cho các kịch bản valid JSON / JSON sai / timeout / stream đứt.
Không có API key thật, không gọi mạng, không đọc/ghi project.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from novel_ai.config import load_config
from novel_ai.core import llm as llm_module
from novel_ai.core.llm import (
    STREAM_INTERRUPTED,
    ChatMessage,
    FakeLLMClient,
    LLMAuthError,
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMError,
    LLMInvalidResponseError,
    LLMNetworkError,
    LLMRequest,
    LLMTimeoutError,
    OpenAICompatibleClient,
    StreamChunk,
    StructuredOutputParseError,
    TransportResponse,
    generate,
    generate_structured,
    llm_config_from_app_config,
    parse_structured_text,
    redact_secrets,
    stream_text,
    validate_llm_config,
)
from novel_ai.core.models import IdeaStateResponse, StructuredOutputError

API_KEY = "sk-test-secret-key-0123456789"
BASE_URL = "http://llm.local/v1"
MODEL = "test-model"


class _Point(BaseModel):
    """Model nhỏ cho test parse; không phụ thuộc payload contract T08."""

    name: str
    count: int = 0


class FakeTransport:
    """Transport ghi lại request và phát kịch bản cố định."""

    def __init__(self, *items: object) -> None:
        self.items = list(items)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> TransportResponse:
        self.calls.append(kwargs)
        if not self.items:
            raise AssertionError("transport đã hết kịch bản")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, TransportResponse)
        return item

    @property
    def bodies(self) -> list[dict[str, object]]:
        bodies = []
        for call in self.calls:
            data = call["data"]
            text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
            bodies.append(json.loads(text))
        return bodies


def _config(**overrides: object) -> LLMConfig:
    data: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": API_KEY,
        "model": MODEL,
        "retry_backoff_seconds": 0.0,
    }
    data.update(overrides)
    return LLMConfig(**data)  # type: ignore[arg-type]


def _request(**overrides: object) -> LLMRequest:
    data: dict[str, object] = {"messages": (ChatMessage(role="user", content="viết thử"),)}
    data.update(overrides)
    return LLMRequest(**data)  # type: ignore[arg-type]


def _chat_body(content: str = "nội dung", *, finish_reason: str = "stop") -> TransportResponse:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    return TransportResponse(status=200, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _delta(text: str, finish: str | None = None) -> str:
    return json.dumps(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]},
        ensure_ascii=False,
    )


def _sse(*chunks: str) -> bytes:
    return "".join(f"data: {chunk}\n\n" for chunk in chunks).encode("utf-8")


# ---------------------------------------------------------------------------
# complete: request/response
# ---------------------------------------------------------------------------


def test_complete_returns_text_and_sends_provider_body() -> None:
    transport = FakeTransport(_chat_body("Xin chào"))
    client = OpenAICompatibleClient(_config(), client=transport)

    response = client.complete(
        _request(prompt_id="co_create.v1", prompt_version="co_create.v1+abcd1234", prompt_hash="abcd1234")
    )

    assert response.text == "Xin chào"
    assert response.finish_reason == "stop"
    assert response.model == MODEL
    assert response.raw is not None

    call = transport.calls[0]
    assert call["url"] == f"{BASE_URL}/chat/completions"
    assert call["timeout"] == 120.0
    assert call["stream"] is False
    body = transport.bodies[0]
    assert body["model"] == MODEL
    assert body["messages"] == [{"role": "user", "content": "viết thử"}]
    assert "stream" not in body
    # Metadata prompt chỉ để debug, không gửi lên provider.
    serialized = json.dumps(body)
    assert "prompt_version" not in serialized
    assert "abcd1234" not in serialized


def test_generation_params_default_from_config_and_request_wins() -> None:
    transport = FakeTransport(_chat_body(), _chat_body())
    client = OpenAICompatibleClient(
        _config(temperature=0.25, max_tokens=777), client=transport
    )

    client.complete(_request())
    client.complete(_request(temperature=1.5, max_tokens=11))

    assert transport.bodies[0]["temperature"] == 0.25
    assert transport.bodies[0]["max_tokens"] == 777
    assert transport.bodies[1]["temperature"] == 1.5
    assert transport.bodies[1]["max_tokens"] == 11


@pytest.mark.parametrize(
    ("native", "json_output", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_response_format_only_when_native_and_json_requested(
    native: bool, json_output: bool, expected: bool
) -> None:
    transport = FakeTransport(_chat_body("{}"))
    client = OpenAICompatibleClient(
        _config(native_structured_output=native), client=transport
    )

    client.complete(_request(json_output=json_output))

    body = transport.bodies[0]
    assert ("response_format" in body) is expected
    if expected:
        assert body["response_format"] == {"type": "json_object"}


def test_missing_api_key_still_calls_local_server_without_authorization_header() -> None:
    transport = FakeTransport(_chat_body("ok"))
    client = OpenAICompatibleClient(_config(api_key=None), client=transport)

    assert client.complete(_request()).text == "ok"
    assert "Authorization" not in transport.calls[0]["headers"]


def test_authorization_header_uses_key_but_is_never_returned() -> None:
    transport = FakeTransport(_chat_body("ok"))
    client = OpenAICompatibleClient(_config(), client=transport)
    client.complete(_request())

    headers = transport.calls[0]["headers"]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in json.dumps(client.config.redacted())


# ---------------------------------------------------------------------------
# complete: phân loại lỗi và retry
# ---------------------------------------------------------------------------


def test_401_raises_auth_error_without_retry_and_without_key() -> None:
    transport = FakeTransport(TransportResponse(status=401, body=b'{"error":"invalid key"}'))
    client = OpenAICompatibleClient(_config(), client=transport)

    with pytest.raises(LLMAuthError) as excinfo:
        client.complete(_request())

    assert excinfo.value.code == "llm_auth"
    assert excinfo.value.retryable is False
    assert len(transport.calls) == 1
    assert API_KEY not in str(excinfo.value)


def test_403_is_also_auth_error() -> None:
    transport = FakeTransport(TransportResponse(status=403, body=b"{}"))
    client = OpenAICompatibleClient(_config(), client=transport)
    with pytest.raises(LLMAuthError):
        client.complete(_request())
    assert len(transport.calls) == 1


def test_5xx_is_retried_then_fails_as_network_error() -> None:
    transport = FakeTransport(*[TransportResponse(status=503, body=b"{}") for _ in range(3)])
    client = OpenAICompatibleClient(_config(max_retries=2), client=transport)

    with pytest.raises(LLMNetworkError) as excinfo:
        client.complete(_request())

    assert excinfo.value.code == "llm_network"
    assert excinfo.value.retryable is True
    assert len(transport.calls) == 3  # 1 lần gọi + max_retries


def test_5xx_retry_can_succeed_on_later_attempt() -> None:
    transport = FakeTransport(TransportResponse(status=500, body=b"{}"), _chat_body("ok"))
    client = OpenAICompatibleClient(_config(max_retries=2), client=transport)

    assert client.complete(_request()).text == "ok"
    assert len(transport.calls) == 2


def test_timeout_is_retried_then_raises_timeout_error() -> None:
    transport = FakeTransport(*[TimeoutError("hết giờ") for _ in range(3)])
    client = OpenAICompatibleClient(_config(max_retries=2), client=transport)

    with pytest.raises(LLMTimeoutError) as excinfo:
        client.complete(_request())

    assert excinfo.value.code == "llm_timeout"
    assert excinfo.value.retryable is True
    assert len(transport.calls) == 3


def test_network_error_is_translated_and_retried() -> None:
    transport = FakeTransport(*[urllib.error.URLError("mất mạng") for _ in range(3)])
    client = OpenAICompatibleClient(_config(max_retries=2), client=transport)

    with pytest.raises(LLMNetworkError) as excinfo:
        client.complete(_request())

    assert excinfo.value.code == "llm_network"
    assert len(transport.calls) == 3


def test_retry_can_be_disabled() -> None:
    transport = FakeTransport(TransportResponse(status=500, body=b"{}"))
    client = OpenAICompatibleClient(_config(max_retries=0), client=transport)

    with pytest.raises(LLMNetworkError):
        client.complete(_request())
    assert len(transport.calls) == 1


def test_400_is_invalid_response_without_retry_and_details_are_redacted() -> None:
    body = json.dumps({"error": f"bad request, key {API_KEY} không hợp lệ"}).encode("utf-8")
    transport = FakeTransport(TransportResponse(status=400, body=body))
    client = OpenAICompatibleClient(_config(), client=transport)

    with pytest.raises(LLMInvalidResponseError) as excinfo:
        client.complete(_request())

    error = excinfo.value
    assert error.code == "llm_invalid_response"
    assert error.retryable is False
    assert len(transport.calls) == 1
    assert API_KEY not in str(error)
    assert API_KEY not in json.dumps(error.details)
    assert "[REDACTED_API_KEY]" in json.dumps(error.details)


def test_non_json_body_is_invalid_response() -> None:
    transport = FakeTransport(
        TransportResponse(status=200, body="<html>không phải JSON</html>".encode("utf-8"))
    )
    client = OpenAICompatibleClient(_config(), client=transport)

    with pytest.raises(LLMInvalidResponseError) as excinfo:
        client.complete(_request())

    assert API_KEY not in json.dumps(excinfo.value.details)


def test_response_without_choices_is_invalid() -> None:
    transport = FakeTransport(
        TransportResponse(status=200, body=json.dumps({"model": MODEL}).encode("utf-8"))
    )
    client = OpenAICompatibleClient(_config(), client=transport)
    with pytest.raises(LLMInvalidResponseError):
        client.complete(_request())


@pytest.mark.parametrize("overrides", [{"base_url": None}, {"model": None}])
def test_missing_config_raises_before_calling_transport(overrides: dict[str, object]) -> None:
    transport = FakeTransport(_chat_body("không bao giờ tới đây"))
    client = OpenAICompatibleClient(_config(**overrides), client=transport)

    with pytest.raises(LLMConfigError):
        client.complete(_request())

    assert transport.calls == []


def test_response_raw_is_redacted_before_caller_can_store_it() -> None:
    payload = {
        "model": MODEL,
        "echo": f"api key {API_KEY}",
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
    }
    transport = FakeTransport(
        TransportResponse(status=200, body=json.dumps(payload).encode("utf-8"))
    )
    client = OpenAICompatibleClient(_config(), client=transport)

    response = client.complete(_request())

    assert response.text == "x"
    assert response.raw is not None
    assert API_KEY not in json.dumps(response.raw)
    assert response.raw["echo"] == "api key [REDACTED_API_KEY]"


def test_invalid_client_object_is_rejected() -> None:
    with pytest.raises(LLMConfigError):
        OpenAICompatibleClient(_config(), client=42)


def test_opener_injection_is_supported() -> None:
    class _RawResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes) -> None:
            self._body = body
            self.closed = False

        def read(self) -> bytes:
            return self._body

        def close(self) -> None:
            self.closed = True

    class _Opener:
        def __init__(self, response: _RawResponse) -> None:
            self.response = response
            self.requests: list[object] = []

        def open(self, request: object, timeout: float | None = None) -> _RawResponse:
            self.requests.append(request)
            return self.response

    body = json.dumps(
        {"model": MODEL, "choices": [{"message": {"content": "qua opener"}, "finish_reason": "stop"}]}
    ).encode("utf-8")
    response = _RawResponse(body)
    opener = _Opener(response)
    client = OpenAICompatibleClient(_config(), client=opener)

    assert client.complete(_request()).text == "qua opener"
    assert response.closed is True
    request = opener.requests[0]
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


def test_stream_emits_deltas_then_completed() -> None:
    transport = FakeTransport(
        TransportResponse(
            status=200,
            body=_sse(_delta("Một "), _delta("hai."), _delta("", finish="stop"), "[DONE]"),
        )
    )
    client = OpenAICompatibleClient(_config(), client=transport)

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "delta", "completed"]
    assert [chunk.text for chunk in chunks] == ["Một ", "hai.", ""]
    assert chunks[-1].finish_reason == "stop"
    assert transport.bodies[0]["stream"] is True


def test_stream_interruption_emits_partial_and_never_completed() -> None:
    def body():
        yield f"data: {_delta('Một ')}\n\n"
        yield f"data: {_delta('hai')}\n\n"
        raise TimeoutError("stream đứt")

    transport = FakeTransport(TransportResponse(status=200, body=body()))
    client = OpenAICompatibleClient(_config(), client=transport)

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "delta", "partial"]
    assert all(chunk.status != "completed" for chunk in chunks)
    assert chunks[-1].text == ""
    assert chunks[-1].raw == {"reason": "llm_timeout", "deltas": 2}


def test_stream_without_terminator_is_partial() -> None:
    transport = FakeTransport(
        TransportResponse(status=200, body=_sse(_delta("Một "), _delta("hai")))
    )
    client = OpenAICompatibleClient(_config(), client=transport)

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "delta", "partial"]
    assert chunks[-1].raw == {"reason": "stream_interrupted", "deltas": 2}


def test_stream_network_error_after_deltas_is_partial() -> None:
    def body():
        yield f"data: {_delta('Một ')}\n\n"
        raise urllib.error.URLError("mất mạng")

    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(TransportResponse(status=200, body=body()))
    )

    chunks = list(client.stream(_request()))

    assert chunks[-1].status == "partial"
    assert chunks[-1].raw is not None
    assert chunks[-1].raw["reason"] == "llm_network"


@pytest.mark.parametrize(
    ("item", "error_type"),
    [
        (TimeoutError("hết giờ"), LLMTimeoutError),
        (urllib.error.URLError("mất mạng"), LLMNetworkError),
    ],
)
def test_stream_error_before_first_delta_raises(item: Exception, error_type: type[LLMError]) -> None:
    def body():
        raise item
        yield ""  # pragma: no cover - không chạy tới

    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(TransportResponse(status=200, body=body()))
    )

    with pytest.raises(error_type):
        list(client.stream(_request()))


def test_stream_auth_error_raises_before_any_chunk() -> None:
    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(TransportResponse(status=401, body=b"{}"))
    )
    with pytest.raises(LLMAuthError):
        list(client.stream(_request()))


def test_stream_malformed_chunk_after_delta_is_partial() -> None:
    def body():
        yield f"data: {_delta('Một ')}\n\n"
        yield "data: {không phải json}\n\n"

    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(TransportResponse(status=200, body=body()))
    )

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "partial"]
    assert chunks[-1].raw is not None
    assert chunks[-1].raw["reason"] == "llm_invalid_response"


def test_stream_text_helper_joins_prose_and_keeps_final_status() -> None:
    completed_client = OpenAICompatibleClient(
        _config(),
        client=FakeTransport(TransportResponse(status=200, body=_sse(_delta("a"), _delta("b"), "[DONE]"))),
    )
    chunks = list(stream_text(completed_client, _request()))
    assert "".join(chunk.text for chunk in chunks) == "ab"
    assert chunks[-1].status == "completed"

    interrupted_client = OpenAICompatibleClient(
        _config(),
        client=FakeTransport(TransportResponse(status=200, body=_sse(_delta("a")))),
    )
    partial_chunks = list(stream_text(interrupted_client, _request()))
    assert "".join(chunk.text for chunk in partial_chunks) == "a"
    assert partial_chunks[-1].status == "partial"


def test_generate_helper_delegates_to_client() -> None:
    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("x")))
    assert generate(client, _request()).text == "x"
    fake = FakeLLMClient(responses=["y"])
    assert generate(fake, _request()).text == "y"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "A", "count": 2}',
        '```json\n{"name": "A", "count": 2}\n```',
        '```\n{"name": "A", "count": 2}\n```',
        '  ```json\n{"name": "A", "count": 2}\n```  ',
    ],
)
def test_parse_structured_text_accepts_plain_and_fenced_json(text: str) -> None:
    assert parse_structured_text(text, _Point) == _Point(name="A", count=2)


def test_parse_structured_text_invalid_json_reports_issue() -> None:
    with pytest.raises(StructuredOutputParseError) as excinfo:
        parse_structured_text("kết quả đây, không phải JSON", _Point)

    error = excinfo.value
    assert error.code == "structured_output_parse"
    assert error.raw_text == "kết quả đây, không phải JSON"
    assert [issue.code for issue in error.errors] == ["invalid_json"]
    assert error.errors[0].path == "/"


def test_parse_structured_text_schema_error_has_path_and_code() -> None:
    with pytest.raises(StructuredOutputParseError) as excinfo:
        parse_structured_text('{"count": "không phải số"}', _Point)

    codes = {(issue.path, issue.code) for issue in excinfo.value.errors}
    assert ("/name", "missing_field") in codes
    assert ("/count", "invalid_type") in codes


def test_parse_structured_text_does_not_mutate_or_accept_anything() -> None:
    """Parse chỉ trả model; không tạo file, không đồng nghĩa artifact accepted."""
    parsed = parse_structured_text('{"name": "A"}', _Point)
    assert isinstance(parsed, BaseModel)
    assert parsed.model_dump() == {"name": "A", "count": 0}


def test_parse_structured_text_works_with_contract_model() -> None:
    text = json.dumps(
        {"message": "chào", "idea_state": {"genre": "suspense", "core_concept": "ý tưởng"}},
        ensure_ascii=False,
    )
    parsed = parse_structured_text(text, IdeaStateResponse)
    assert parsed.idea_state.genre == "suspense"
    assert parsed.idea_state.core_concept == "ý tưởng"


def test_generate_structured_returns_model_and_raw_text() -> None:
    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(_chat_body('{"name": "A", "count": 1}'))
    )

    parsed, raw, error = generate_structured(client, _request(json_output=True), _Point)

    assert parsed == _Point(name="A", count=1)
    assert raw == '{"name": "A", "count": 1}'
    assert error is None


def test_generate_structured_returns_retryable_error_record_for_bad_json() -> None:
    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("không phải JSON")))

    parsed, raw, error = generate_structured(client, _request(json_output=True), _Point)

    assert parsed is None
    assert raw == "không phải JSON"
    assert isinstance(error, StructuredOutputError)
    assert error.retryable is True
    assert error.error_id.startswith("err_")
    assert [issue.code for issue in error.errors] == ["invalid_json"]
    # Adapter không tự lưu raw và không gắn artifact nào.
    assert error.raw_output_ref is None
    assert error.artifact_id is None
    assert datetime.fromisoformat(error.created_at)


def test_generate_structured_repairs_at_most_once() -> None:
    calls: list[str] = []
    raw = 'kết quả: {"name": "A"}'

    def repair(text: str) -> str:
        calls.append(text)
        return '{"name": "A"}'

    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body(raw)))
    parsed, text, error = generate_structured(
        client, _request(), _Point, repair=repair, repair_attempts=5
    )

    assert parsed == _Point(name="A")
    assert text == '{"name": "A"}'
    assert error is None
    assert calls == [raw]  # tối đa một lần dù cấu hình lớn hơn


def test_generate_structured_returns_error_when_repair_fails() -> None:
    calls: list[str] = []

    def repair(text: str) -> str:
        calls.append(text)
        return "vẫn không phải JSON"

    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("hỏng")))
    parsed, raw, error = generate_structured(
        client, _request(), _Point, repair=repair, repair_attempts=1
    )

    assert parsed is None
    assert raw == "hỏng"
    assert error is not None and error.retryable is True
    assert calls == ["hỏng"]


def test_generate_structured_skips_repair_when_not_configured() -> None:
    calls: list[str] = []

    def repair(text: str) -> str:
        calls.append(text)
        return '{"name": "A"}'

    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("hỏng")))
    parsed, _, error = generate_structured(
        client, _request(), _Point, repair=repair, repair_attempts=0
    )

    assert parsed is None and error is not None
    assert calls == []

    client2 = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("hỏng")))
    parsed2, _, error2 = generate_structured(client2, _request(), _Point, repair=None, repair_attempts=1)
    assert parsed2 is None and error2 is not None


def test_generate_structured_propagates_network_errors() -> None:
    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(TransportResponse(status=401, body=b"{}"))
    )
    with pytest.raises(LLMAuthError):
        generate_structured(client, _request(), _Point)


def test_structured_output_generation_writes_no_raw_file(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(_config(), client=FakeTransport(_chat_body("hỏng")))
    generate_structured(client, _request(), _Point)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_redact_secrets_hides_key() -> None:
    config = _config()
    assert redact_secrets(f"header gửi {API_KEY} trong log", config) == (
        "header gửi [REDACTED_API_KEY] trong log"
    )
    assert redact_secrets("không có secret", config) == "không có secret"
    assert redact_secrets("giữ nguyên", _config(api_key=None)) == "giữ nguyên"
    assert redact_secrets("giữ nguyên", _config(api_key="")) == "giữ nguyên"


def test_config_repr_and_redacted_never_contain_key(caplog: pytest.LogCaptureFixture) -> None:
    config = _config()

    assert API_KEY not in repr(config)
    assert config.redacted()["api_key"] == "set"
    assert _config(api_key=None).redacted()["api_key"] == "not set"
    assert "api_key='set'" in repr(config)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("novel_ai.test").debug("config=%s", config)
    assert API_KEY not in caplog.text


@pytest.mark.parametrize(
    ("overrides", "env_hint"),
    [
        ({"base_url": None}, "NOVEL_AI_API_BASE_URL"),
        ({"api_key": None}, "NOVEL_AI_API_KEY"),
        ({"model": None}, "NOVEL_AI_MODEL"),
    ],
)
def test_validate_llm_config_reports_each_missing_field_without_values(
    overrides: dict[str, object], env_hint: str
) -> None:
    problems = validate_llm_config(_config(**overrides))

    assert any(env_hint in problem for problem in problems)
    assert API_KEY not in " ".join(problems)


def test_validate_llm_config_accepts_complete_config() -> None:
    assert validate_llm_config(_config()) == []


def test_validate_llm_config_reports_invalid_numbers() -> None:
    problems = validate_llm_config(_config(timeout_seconds=0, max_tokens=0, max_retries=-1))
    joined = " ".join(problems)
    assert "timeout_seconds" in joined
    assert "max_tokens" in joined
    assert "max_retries" in joined


def test_llm_config_from_app_config_maps_fields() -> None:
    app_config = load_config(
        env={
            "NOVEL_AI_API_BASE_URL": BASE_URL,
            "NOVEL_AI_API_KEY": API_KEY,
            "NOVEL_AI_MODEL": MODEL,
            "NOVEL_AI_LLM_TIMEOUT_SECONDS": "30",
            "NOVEL_AI_LLM_TEMPERATURE": "0.2",
            "NOVEL_AI_LLM_MAX_TOKENS": "512",
            "NOVEL_AI_LLM_NATIVE_STRUCTURED_OUTPUT": "true",
        }
    )

    config = llm_config_from_app_config(app_config)

    assert (config.base_url, config.api_key, config.model) == (BASE_URL, API_KEY, MODEL)
    assert (config.timeout_seconds, config.temperature, config.max_tokens) == (30.0, 0.2, 512)
    assert config.native_structured_output is True
    assert validate_llm_config(config) == []


# ---------------------------------------------------------------------------
# FakeLLMClient (stream + kịch bản lỗi)
# ---------------------------------------------------------------------------


def test_fake_client_stream_deltas_then_completed() -> None:
    client = FakeLLMClient()
    client.queue_stream("Một ", "hai.")

    chunks = list(client.stream(_request()))

    assert [(chunk.status, chunk.text) for chunk in chunks] == [
        ("delta", "Một "),
        ("delta", "hai."),
        ("completed", ""),
    ]
    assert len(client.calls) == 1


def test_fake_client_stream_interruption_emits_partial_not_completed() -> None:
    client = FakeLLMClient()
    client.queue_stream("Một ", STREAM_INTERRUPTED)

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "partial"]
    assert chunks[-1].finish_reason == "interrupted"
    assert chunks[-1].raw == {"reason": "stream_interrupted"}


def test_fake_client_stream_raises_scripted_exception_at_that_turn() -> None:
    client = FakeLLMClient()
    client.queue_stream(LLMTimeoutError("hết giờ"))

    with pytest.raises(LLMTimeoutError):
        list(client.stream(_request()))

    assert len(client.calls) == 1


def test_fake_client_stream_accepts_explicit_terminal_chunk() -> None:
    client = FakeLLMClient()
    client.queue_stream(
        StreamChunk(text="a", status="delta"),
        StreamChunk(text="", status="partial", finish_reason="length"),
        "không bao giờ phát",
    )

    chunks = list(client.stream(_request()))

    assert [chunk.status for chunk in chunks] == ["delta", "partial"]


def test_fake_client_stream_falls_back_to_response_queue() -> None:
    client = FakeLLMClient(responses=["prose hoàn chỉnh"])

    chunks = list(client.stream(_request()))

    assert [(chunk.status, chunk.text) for chunk in chunks] == [
        ("delta", "prose hoàn chỉnh"),
        ("completed", ""),
    ]


def test_fake_client_stream_exhausted_queue_raises_llm_error() -> None:
    with pytest.raises(LLMError):
        list(FakeLLMClient().stream(_request()))


def test_fake_client_queue_stream_accepts_single_list() -> None:
    client = FakeLLMClient()
    client.queue_stream(["a", "b"])

    texts = [chunk.text for chunk in client.stream(_request()) if chunk.status == "delta"]
    assert texts == ["a", "b"]


def test_fake_client_complete_and_stream_share_call_log() -> None:
    client = FakeLLMClient(responses=["một"])
    client.queue_stream("hai")
    client.complete(_request())
    list(client.stream(_request()))
    assert len(client.calls) == 2


def test_fake_client_implements_extended_protocol() -> None:
    assert isinstance(FakeLLMClient(), LLMClient)


# ---------------------------------------------------------------------------
# Ranh giới trách nhiệm
# ---------------------------------------------------------------------------


def test_adapter_writes_no_file_and_has_no_service_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = OpenAICompatibleClient(
        _config(), client=FakeTransport(_chat_body("{}"), _chat_body("không phải JSON"))
    )

    client.complete(_request())
    generate_structured(client, _request(), _Point)

    assert list(tmp_path.iterdir()) == []

    source = Path(llm_module.__file__).read_text(encoding="utf-8")
    assert "novel_ai.services" not in source
    assert "novel_ai.core.storage" not in source
    assert "novel_ai.core.project" not in source


def test_error_codes_are_stable() -> None:
    assert LLMAuthError.code == "llm_auth"
    assert LLMNetworkError.code == "llm_network"
    assert LLMTimeoutError.code == "llm_timeout"
    assert LLMInvalidResponseError.code == "llm_invalid_response"
    assert LLMConfigError.code == "llm_config"
    assert StructuredOutputParseError.code == "structured_output_parse"
    for error_type in (
        LLMAuthError,
        LLMNetworkError,
        LLMTimeoutError,
        LLMInvalidResponseError,
        LLMConfigError,
        StructuredOutputParseError,
    ):
        assert issubclass(error_type, LLMError)

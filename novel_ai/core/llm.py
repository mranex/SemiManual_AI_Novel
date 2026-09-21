"""Interface LLM, adapter OpenAI-compatible và xử lý lỗi (T07 + T11).

T07 đặt interface tối thiểu (`ChatMessage`, `LLMRequest`, `LLMResponse`,
`LLMError`, `LLMClient`, `FakeLLMClient`) để test harness chạy offline. T11 mở
rộng chính file này chứ không viết lại service:

- `LLMConfig` + `llm_config_from_app_config` + `validate_llm_config`: cấu hình
  endpoint/model/timeout/tham số sinh, kiểm tra **trước** khi gọi, không bao giờ
  nêu giá trị secret.
- `OpenAICompatibleClient`: POST `{base_url}/chat/completions` bằng stdlib
  (`urllib.request`), không thêm dependency; retry có giới hạn cho
  network/timeout/5xx, không retry auth/4xx; timeout lấy từ config; hỗ trợ
  `response_format={"type":"json_object"}` khi provider native structured output.
- Phân loại lỗi: `LLMAuthError`, `LLMNetworkError`, `LLMTimeoutError`,
  `LLMInvalidResponseError`, `LLMConfigError`, `StructuredOutputParseError`
  (mọi class có `code` ổn định).
- `StreamChunk` + `stream`: stream đứt phát chunk cuối `status="partial"`,
  **không** bao giờ thành `completed`.
- `parse_structured_text`, `generate_structured`: parse/validate JSON trả về
  model; repair tối đa một lần nếu được cấu hình; **không** tự lưu raw và
  **không** tự chạy bước tiếp.
- `redact_secrets`: che API key nếu vô tình lọt vào text/log.

Luật không được nới trong file này:

- Adapter **không** ghi canon, không đọc/ghi project, không gọi service, không
  tự accept. Nó chỉ đổi `messages` thành raw response/stream chunk.
- API key chỉ nằm trong header `Authorization`; không log header, không echo key
  trong message lỗi, không nhét key vào `LLMResponse.raw`.
- `LLMResponse.text` là output của model, không phải artifact đã accept.

Test offline: inject `client=` (transport/opener) vào `OpenAICompatibleClient`,
hoặc dùng `FakeLLMClient` với kịch bản valid JSON, JSON sai, timeout, stream đứt.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from novel_ai.core.models import (
    StructuredOutputError,
    ValidationIssue,
    generate_error_id,
    now_iso,
)
from novel_ai.core.validation import issues_from_pydantic_error

if TYPE_CHECKING:  # pragma: no cover - chỉ dùng cho type checker
    from novel_ai.config import AppConfig

__all__ = [
    "ChatMessage",
    "FakeLLMClient",
    "LLMClient",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "LLMInvalidResponseError",
    "LLMNetworkError",
    "LLMRequest",
    "LLMResponse",
    "LLMAuthError",
    "LLMTimeoutError",
    "OpenAICompatibleClient",
    "STREAM_INTERRUPTED",
    "StreamChunk",
    "StructuredOutputParseError",
    "TransportResponse",
    "generate",
    "generate_structured",
    "llm_config_from_app_config",
    "parse_structured_text",
    "redact_secrets",
    "stream_text",
    "urllib_transport",
    "validate_llm_config",
]


# ---------------------------------------------------------------------------
# Message, request, response (API T07 giữ nguyên)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatMessage:
    """Một message theo format chat completion."""

    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class LLMRequest:
    """Yêu cầu sinh văn bản. Service chọn prompt/context, adapter chỉ gọi API.

    `temperature`/`max_tokens` để `None` nghĩa là lấy từ `LLMConfig` (không
    hardcode trong adapter). Các field `prompt_*` chỉ là metadata debug: chúng
    **không** được gửi lên provider, và không chứa secret.
    """

    messages: tuple[ChatMessage, ...]
    temperature: float | None = None
    max_tokens: int | None = None
    model: str | None = None
    #: True khi action cần JSON; dùng để bật `response_format` nếu provider hỗ trợ.
    json_output: bool = False
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """Kết quả thô từ provider. Không phải artifact đã accept."""

    text: str
    model: str | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamChunk:
    """Một mẩu stream.

    Quy ước: chunk `delta` mang text tăng dần; chunk kết thúc (`completed`,
    `partial`, `error`) mang `text=""` và mô tả trạng thái trong `raw`. Vì vậy
    caller cộng dồn text của các chunk `delta` và đọc `status` của chunk cuối để
    biết stream có hoàn tất hay không. Stream đứt là `partial`, không bao giờ là
    `completed`.
    """

    text: str
    status: Literal["delta", "completed", "partial", "error"]
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Lỗi phân loại
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Lỗi gọi LLM (mạng, timeout, HTTP, stream đứt, provider trả lỗi).

    `code` ổn định để service/UI phân loại mà không parse message; `retryable`
    cho biết adapter có được phép thử lại. Message **không** chứa API key hay
    header nhạy cảm.
    """

    code = "llm_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})


class LLMAuthError(LLMError):
    """Provider từ chối xác thực (401/403). Không retry."""

    code = "llm_auth"
    retryable = False


class LLMNetworkError(LLMError):
    """Lỗi mạng/kết nối hoặc provider lỗi tạm thời (5xx). Có thể retry."""

    code = "llm_network"
    retryable = True


class LLMTimeoutError(LLMError):
    """Quá `timeout_seconds` khi gọi hoặc khi đọc stream. Có thể retry."""

    code = "llm_timeout"
    retryable = True


class LLMInvalidResponseError(LLMError):
    """Provider trả HTTP lạ hoặc body không đúng shape chat completion. Không retry."""

    code = "llm_invalid_response"
    retryable = False


class LLMConfigError(LLMError):
    """Thiếu/sai cấu hình LLM. Phải báo trước khi gửi request."""

    code = "llm_config"
    retryable = False


class StructuredOutputParseError(LLMError):
    """Output LLM không parse/không khớp schema. Mang raw text và lỗi theo path."""

    code = "structured_output_parse"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        raw_text: str = "",
        errors: Iterable[ValidationIssue] | None = None,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.raw_text = raw_text
        self.errors: list[ValidationIssue] = list(errors or [])


# ---------------------------------------------------------------------------
# Cấu hình LLM
# ---------------------------------------------------------------------------

#: Thông báo cấu hình thiếu chỉ nêu tên biến môi trường, không nêu giá trị.
ENV_HINT_BASE_URL = "NOVEL_AI_API_BASE_URL"
ENV_HINT_API_KEY = "NOVEL_AI_API_KEY"
ENV_HINT_MODEL = "NOVEL_AI_MODEL"


@dataclass(frozen=True, repr=False)
class LLMConfig:
    """Cấu hình adapter. Không bao giờ được ghi vào project truyện/snapshot/fixture.

    `__repr__` được viết lại để API key không lộ qua log/repr; dùng `redacted()`
    khi cần in cấu hình.
    """

    base_url: str | None
    api_key: str | None
    model: str | None
    timeout_seconds: float = 120.0
    temperature: float = 0.7
    max_tokens: int = 4096
    native_structured_output: bool = False
    max_retries: int = 2
    format_repair_attempts: int = 1
    #: Nghỉ giữa các lần retry (giây), tăng theo cấp số nhân và có trần.
    retry_backoff_seconds: float = 0.5

    @property
    def has_api_key(self) -> bool:
        return bool((self.api_key or "").strip())

    def redacted(self) -> dict[str, Any]:
        """Bản an toàn để in log/UI: API key chỉ còn `set`/`not set`."""
        return {
            "base_url": self.base_url or "not set",
            "api_key": "set" if self.has_api_key else "not set",
            "model": self.model or "not set",
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "native_structured_output": self.native_structured_output,
            "max_retries": self.max_retries,
            "format_repair_attempts": self.format_repair_attempts,
        }

    def __repr__(self) -> str:
        parts = ", ".join(f"{key}={value!r}" for key, value in self.redacted().items())
        return f"LLMConfig({parts})"


def llm_config_from_app_config(config: AppConfig) -> LLMConfig:
    """Chuyển app config thành cấu hình adapter. Không copy secret ra chỗ khác."""
    return LLMConfig(
        base_url=config.api_base_url,
        api_key=config.api_key,
        model=config.model,
        timeout_seconds=config.llm_timeout_seconds,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
        native_structured_output=config.llm_native_structured_output,
    )


def validate_llm_config(config: LLMConfig) -> list[str]:
    """Danh sách vấn đề cấu hình, rỗng nghĩa là dùng được.

    Message nêu **tên biến môi trường** cần đặt, không nêu giá trị, để gọi trước
    generation mà không làm lộ secret vào log/UI.
    """
    problems: list[str] = []
    if not (config.base_url or "").strip():
        problems.append(f"Thiếu endpoint LLM: đặt {ENV_HINT_BASE_URL} (ví dụ http://localhost:1234/v1).")
    if not config.has_api_key:
        problems.append(f"Thiếu API key LLM: đặt {ENV_HINT_API_KEY}.")
    if not (config.model or "").strip():
        problems.append(f"Thiếu model LLM: đặt {ENV_HINT_MODEL}.")
    if config.timeout_seconds <= 0:
        problems.append("LLMConfig.timeout_seconds phải lớn hơn 0.")
    if config.max_tokens <= 0:
        problems.append("LLMConfig.max_tokens phải lớn hơn 0.")
    if config.max_retries < 0:
        problems.append("LLMConfig.max_retries phải >= 0.")
    if config.format_repair_attempts < 0:
        problems.append("LLMConfig.format_repair_attempts phải >= 0.")
    return problems


def redact_secrets(text: str, config: LLMConfig) -> str:
    """Che API key nếu vô tình xuất hiện trong `text` (message lỗi, log, raw)."""
    value = text if isinstance(text, str) else str(text)
    key = (config.api_key or "").strip()
    if not key or key not in value:
        return value
    return value.replace(key, "[REDACTED_API_KEY]")


def _redact_value(value: Any, config: LLMConfig) -> Any:
    """Che key trong cấu trúc raw trước khi trả cho caller (raw có thể bị lưu)."""
    if isinstance(value, str):
        return redact_secrets(value, config)
    if isinstance(value, Mapping):
        return {key: _redact_value(item, config) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, config) for item in value]
    return value


# ---------------------------------------------------------------------------
# Transport (stdlib; inject được để test offline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportResponse:
    """Kết quả transport ở mức thấp.

    `body` là bytes (JSON) hoặc iterable các dòng (SSE streaming). Với streaming
    thật, `body` có thể là file-like của `urllib` và adapter sẽ tự `close()`.
    """

    status: int
    body: Any = b""
    headers: Mapping[str, str] = field(default_factory=dict)


#: Transport nhận đúng các keyword này; trả `TransportResponse`.
Transport = Callable[..., TransportResponse]


def urllib_transport(
    url: str,
    *,
    data: bytes,
    headers: Mapping[str, str],
    timeout: float,
    stream: bool = False,
) -> TransportResponse:
    """Transport mặc định dùng `urllib.request` (không thêm dependency).

    HTTP error được chuyển thành `TransportResponse` với status thật để adapter
    phân loại; lỗi timeout/mạng để nguyên và adapter dịch thành `LLMError` tương
    ứng. Không đọc/echo header nhạy cảm.
    """
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            body: Any = exc.read()
        except Exception:  # pragma: no cover - body lỗi không đọc được
            body = b""
        return TransportResponse(status=int(exc.code), body=body, headers=dict(exc.headers or {}))
    status = int(getattr(response, "status", 200) or 200)
    if stream and 200 <= status < 300:
        # Trả file-like để đọc dần; adapter chịu trách nhiệm close.
        return TransportResponse(status=status, body=response, headers=dict(response.headers))
    try:
        body = response.read()
    finally:
        response.close()
    return TransportResponse(status=status, body=body, headers=dict(response.headers))


def _coerce_transport(client: Any | None) -> Transport:
    """Nhận transport callable hoặc opener kiểu `urllib` (có `.open`)."""
    if client is None:
        return urllib_transport
    if callable(client):
        return client
    opener = getattr(client, "open", None)
    if callable(opener):

        def _opener_transport(
            url: str,
            *,
            data: bytes,
            headers: Mapping[str, str],
            timeout: float,
            stream: bool = False,
        ) -> TransportResponse:
            request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
            response = opener(request, timeout=timeout)
            status = int(getattr(response, "status", 200) or 200)
            if stream and 200 <= status < 300:
                return TransportResponse(status=status, body=response, headers=dict(response.headers))
            try:
                body = response.read()
            finally:
                response.close()
            return TransportResponse(status=status, body=body, headers=dict(response.headers))

        return _opener_transport
    raise LLMConfigError(
        "`client` phải là transport callable hoặc opener có `.open`; "
        f"thấy {type(client).__name__}."
    )


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Interface tối thiểu mà service phụ thuộc."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Sinh một response hoàn chỉnh hoặc raise `LLMError`."""
        ...

    def stream(self, request: LLMRequest) -> Iterator[StreamChunk]:
        """Phát stream chunk; chunk cuối là `completed`/`partial`/`error`."""
        ...


# ---------------------------------------------------------------------------
# Adapter OpenAI-compatible
# ---------------------------------------------------------------------------


class OpenAICompatibleClient:
    """Adapter cho mọi server expose OpenAI-compatible `/chat/completions`.

    `client` cho phép inject transport/opener để test offline (không gọi mạng).
    Cấu hình thiếu được báo bằng `LLMConfigError` khi gọi; dùng
    `validate_llm_config` để kiểm tra trước generation.
    """

    def __init__(self, config: LLMConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._transport: Transport = _coerce_transport(client)

    # -- public -------------------------------------------------------------
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Gọi một lần (có retry giới hạn) và trả `LLMResponse`."""
        payload = self._encode_body(request, stream=False)
        response = self._post_with_retry(payload, stream=False)
        data = self._decode_json(response)
        return self._to_llm_response(data)

    def stream(self, request: LLMRequest) -> Iterator[StreamChunk]:
        """Phát `StreamChunk` cho request streaming.

        Delta được phát dần. Nếu stream đứt giữa đường (timeout/mạng/body lỗi),
        chunk cuối là `partial` và text đã phát **không** bị coi là hoàn tất.
        Lỗi xảy ra trước khi có delta nào thì raise lỗi đã phân loại.
        """
        payload = self._encode_body(request, stream=True)
        response = self._post_with_retry(payload, stream=True)
        body = response.body
        finish_reason: str | None = None
        saw_terminator = False
        deltas = 0
        try:
            for line in self._iter_lines(body):
                parsed = self._parse_sse_line(line, response_status=response.status)
                if parsed is _SSE_DONE:
                    saw_terminator = True
                    break
                if parsed is None:
                    continue
                text, chunk_finish = parsed
                if text:
                    deltas += 1
                    yield StreamChunk(text=text, status="delta")
                if chunk_finish:
                    finish_reason = chunk_finish
        except (TimeoutError, socket.timeout) as exc:
            if deltas:
                yield self._partial_chunk("llm_timeout", deltas)
                return
            raise LLMTimeoutError(
                f"LLM timeout sau {self.config.timeout_seconds}s khi đọc stream."
            ) from exc
        except LLMError as exc:
            if deltas:
                yield self._partial_chunk(exc.code, deltas)
                return
            raise
        except (urllib.error.URLError, OSError) as exc:
            if deltas:
                yield self._partial_chunk("llm_network", deltas)
                return
            raise LLMNetworkError(
                f"Lỗi mạng khi đọc stream từ provider: {type(exc).__name__}."
            ) from exc
        finally:
            close = getattr(body, "close", None)
            if callable(close):  # pragma: no cover - chỉ có với transport urllib thật
                close()

        if finish_reason is not None or saw_terminator:
            yield StreamChunk(
                text="",
                status="completed",
                finish_reason=finish_reason or "stop",
                raw={"deltas": deltas},
            )
            return
        # Hết body mà không có tín hiệu kết thúc: stream đứt, không phải hoàn tất.
        yield self._partial_chunk("stream_interrupted", deltas)

    # -- nội bộ -------------------------------------------------------------
    def _chat_completions_url(self) -> str:
        base = (self.config.base_url or "").strip()
        if not base:
            raise LLMConfigError(
                f"Thiếu endpoint LLM: đặt {ENV_HINT_BASE_URL} trước khi gọi."
            )
        return f"{base.rstrip('/')}/chat/completions"

    def _require_model(self, request: LLMRequest) -> str:
        model = (request.model or self.config.model or "").strip()
        if not model:
            raise LLMConfigError(f"Thiếu model LLM: đặt {ENV_HINT_MODEL} trước khi gọi.")
        return model

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        key = (self.config.api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _encode_body(self, request: LLMRequest, *, stream: bool) -> bytes:
        model = self._require_model(request)
        body: dict[str, Any] = {
            "model": model,
            "messages": [message.as_dict() for message in request.messages],
            "temperature": (
                self.config.temperature if request.temperature is None else request.temperature
            ),
            "max_tokens": (
                self.config.max_tokens if request.max_tokens is None else request.max_tokens
            ),
        }
        if stream:
            body["stream"] = True
        if self.config.native_structured_output and request.json_output:
            body["response_format"] = {"type": "json_object"}
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _post_with_retry(self, payload: bytes, *, stream: bool) -> TransportResponse:
        url = self._chat_completions_url()
        headers = self._headers(stream=stream)
        attempts = max(1, int(self.config.max_retries) + 1)
        last_error: LLMError | None = None
        for attempt in range(attempts):
            if attempt:
                delay = min(
                    max(self.config.retry_backoff_seconds, 0.0) * (2 ** (attempt - 1)),
                    8.0,
                )
                if delay > 0:
                    time.sleep(delay)
            try:
                response = self._transport(
                    url=url,
                    data=payload,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                    stream=stream,
                )
            except LLMError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                last_error = LLMTimeoutError(
                    f"LLM timeout sau {self.config.timeout_seconds}s khi gọi {url}.",
                    details={"attempt": attempt + 1},
                )
                last_error.__cause__ = exc
                continue
            except (urllib.error.URLError, OSError) as exc:
                last_error = LLMNetworkError(
                    f"Lỗi mạng khi gọi {url}: {type(exc).__name__}.",
                    details={"attempt": attempt + 1},
                )
                last_error.__cause__ = exc
                continue

            status = int(getattr(response, "status", 0) or 0)
            if 200 <= status < 300:
                return response
            error = self._error_for_status(status, body=response.body)
            if not error.retryable:
                raise error
            last_error = error
        assert last_error is not None  # attempts >= 1 nên vòng lặp luôn set
        raise last_error

    def _error_for_status(self, status: int, *, body: Any) -> LLMError:
        details = {"status": status}
        if status in (401, 403):
            return LLMAuthError(
                f"Provider từ chối xác thực (HTTP {status}). Kiểm tra {ENV_HINT_API_KEY}.",
                details=details,
            )
        if status == 408:
            return LLMTimeoutError(
                f"Provider báo timeout (HTTP {status}).", details=details
            )
        if status >= 500:
            return LLMNetworkError(
                f"Provider lỗi tạm thời (HTTP {status}); đã thử lại theo max_retries.",
                details=details,
            )
        snippet = self._body_snippet(body)
        return LLMInvalidResponseError(
            f"Provider trả HTTP {status} không mong đợi.",
            details={**details, "body": snippet},
        )

    def _body_snippet(self, body: Any, *, limit: int = 200) -> str:
        """Trích ngắn body để debug, đã che secret; không nằm trong message lỗi."""
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "replace")
        elif isinstance(body, str):
            text = body
        else:
            return ""
        return redact_secrets(text[:limit], self.config)

    def _decode_json(self, response: TransportResponse) -> Any:
        body = response.body
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "replace")
        elif isinstance(body, str):
            text = body
        else:
            text = "".join(self._iter_lines(body))
        try:
            return json.loads(text) if text.strip() else None
        except ValueError as exc:
            raise LLMInvalidResponseError(
                f"Provider trả body không phải JSON hợp lệ (HTTP {response.status}).",
                details={"status": response.status, "body": redact_secrets(text[:200], self.config)},
            ) from exc

    def _to_llm_response(self, data: Any) -> LLMResponse:
        if not isinstance(data, Mapping):
            raise LLMInvalidResponseError(
                "Provider response không phải object JSON chat completion.",
                details={"body": redact_secrets(json.dumps(data, ensure_ascii=False)[:200], self.config)},
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMInvalidResponseError(
                "Provider response thiếu `choices`.",
                details={"body": redact_secrets(json.dumps(data, ensure_ascii=False)[:200], self.config)},
            )
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        text = message.get("content")
        if text is None:
            text = first.get("text", "")
        if not isinstance(text, str):
            raise LLMInvalidResponseError(
                "`choices[0].message.content` không phải chuỗi.",
                details={"status": first.get("finish_reason")},
            )
        finish_reason = first.get("finish_reason")
        raw = _redact_value(data, self.config)
        return LLMResponse(
            text=text,
            model=data.get("model") or self.config.model,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            raw=raw if isinstance(raw, dict) else None,
        )

    # -- stream helpers -----------------------------------------------------
    def _iter_lines(self, body: Any) -> Iterator[str]:
        if body is None:
            return
        if isinstance(body, (bytes, bytearray)):
            yield from bytes(body).decode("utf-8", "replace").splitlines()
            return
        if isinstance(body, str):
            yield from body.splitlines()
            return
        for item in body:
            if isinstance(item, (bytes, bytearray)):
                yield from bytes(item).decode("utf-8", "replace").splitlines()
            else:
                yield from str(item).splitlines()

    def _parse_sse_line(self, line: str, *, response_status: int) -> Any:
        """Trả `(text, finish_reason)`, `_SSE_DONE`, hoặc None nếu bỏ qua dòng."""
        text = line.strip()
        if not text or text.startswith(":"):
            return None
        if text.startswith("data:"):
            text = text[len("data:") :].strip()
        elif not text.startswith("{"):
            return None
        if text == "[DONE]":
            return _SSE_DONE
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LLMInvalidResponseError(
                f"Stream chunk không phải JSON hợp lệ (HTTP {response_status}).",
                details={"body": redact_secrets(text[:200], self.config)},
            ) from exc
        if not isinstance(data, Mapping):
            return None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), Mapping) else {}
        content = delta.get("content", first.get("text", ""))
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        finish = first.get("finish_reason")
        return content, (finish if isinstance(finish, str) else None)

    def _partial_chunk(self, reason: str, deltas: int) -> StreamChunk:
        return StreamChunk(
            text="",
            status="partial",
            finish_reason="interrupted",
            raw={"reason": reason, "deltas": deltas},
        )


class _SseDone:
    def __repr__(self) -> str:  # pragma: no cover - debug
        return "[DONE]"


_SSE_DONE = _SseDone()


# ---------------------------------------------------------------------------
# Helper cấp cao
# ---------------------------------------------------------------------------


def generate(client: LLMClient, request: LLMRequest) -> LLMResponse:
    """Gọi `complete` qua interface; giữ service không phụ thuộc adapter cụ thể."""
    return client.complete(request)


def stream_text(client: LLMClient, request: LLMRequest) -> Iterator[StreamChunk]:
    """Chỉ phát phần có nội dung: delta rỗng bị bỏ, chunk kết thúc vẫn giữ.

    Nhờ vậy `"".join(chunk.text for chunk in stream_text(...))` là prose nhận
    được, còn `status` của chunk cuối cho biết `completed` hay `partial`.
    """
    for chunk in client.stream(request):
        if chunk.status == "delta" and not chunk.text:
            continue
        yield chunk


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

TModel = TypeVar("TModel", bound=BaseModel)

#: ```json ... ``` hoặc ``` ... ``` bọc quanh JSON.
_FENCE_RE = re.compile(
    r"^\s*```[A-Za-z0-9_.+-]*\s*\n?(?P<body>.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group("body").strip()
    if stripped.startswith("```"):
        # Fence mở mà thiếu fence đóng: bỏ dòng đầu và dấu ``` còn lại nếu có.
        body = "\n".join(stripped.splitlines()[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -len("```")]
        return body.strip()
    return stripped


def parse_structured_text(text: str, model_cls: type[TModel]) -> TModel:
    """Parse JSON (đã strip code fence) rồi validate bằng `model_cls`.

    Lỗi JSON hoặc lỗi schema đều raise `StructuredOutputParseError` mang
    `raw_text` và `errors` có `path`/`code` (dùng
    `novel_ai.core.validation.issues_from_pydantic_error`). Hàm này không sửa
    dữ liệu, không lưu raw và không accept gì.
    """
    if not isinstance(text, str):
        raise StructuredOutputParseError(
            "Output LLM phải là chuỗi để parse structured output.",
            raw_text=str(text),
            errors=[
                ValidationIssue(
                    path="/",
                    code="invalid_type",
                    message="Output LLM không phải chuỗi.",
                )
            ],
        )
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except ValueError as exc:
        raise StructuredOutputParseError(
            f"Output LLM không phải JSON hợp lệ: {exc}. Không merge candidate; raw được giữ để người dùng xử lý.",
            raw_text=text,
            errors=[
                ValidationIssue(
                    path="/",
                    code="invalid_json",
                    message=f"Không parse được JSON: {exc}",
                )
            ],
        ) from exc
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        issues = issues_from_pydantic_error(exc, base_path="")
        summary = "; ".join(f"{issue.path} [{issue.code}]" for issue in issues) or "schema không khớp"
        raise StructuredOutputParseError(
            f"Output LLM đúng JSON nhưng sai schema: {summary}. Không merge candidate.",
            raw_text=text,
            errors=issues,
        ) from exc


def _structured_error(exc: StructuredOutputParseError) -> StructuredOutputError:
    """Bọc lỗi parse thành record để service lưu/show; adapter không tự lưu raw."""
    issues = list(exc.errors) or [
        ValidationIssue(path="/", code="invalid_json", message=str(exc))
    ]
    return StructuredOutputError(
        error_id=generate_error_id(),
        errors=issues,
        created_at=now_iso(),
        retryable=True,
    )


def generate_structured(
    client: LLMClient,
    request: LLMRequest,
    model_cls: type[TModel],
    *,
    repair: Callable[[str], str] | None = None,
    repair_attempts: int = 0,
) -> tuple[TModel | None, str, StructuredOutputError | None]:
    """Gọi LLM rồi parse/validate structured output.

    Trả `(parsed_or_None, raw_text, error_or_None)`:

    - parse thành công: `(model, text_đã_parse, None)`;
    - parse fail: `(None, raw_text, StructuredOutputError(retryable=True))`.

    `repair` chỉ chạy tối đa **một** lần và chỉ khi `repair_attempts >= 1`; nếu
    repair thành công, phần tử thứ hai là text đã repair (text thực sự parse
    được). Hàm **không** tự lưu raw, **không** accept, **không** chạy bước tiếp:
    caller/service quyết định lưu raw và retry.

    Lỗi mạng/auth/timeout từ client được raise nguyên trạng (không bọc thành
    `StructuredOutputError`) vì chưa có output nào để nói về schema.
    """
    response = client.complete(request)
    raw_text = response.text if isinstance(response.text, str) else str(response.text)

    try:
        return parse_structured_text(raw_text, model_cls), raw_text, None
    except StructuredOutputParseError as first_error:
        allowed = max(0, min(int(repair_attempts), 1))
        if repair is None or allowed == 0:
            return None, raw_text, _structured_error(first_error)
        repaired = repair(raw_text)
        if not isinstance(repaired, str) or not repaired.strip() or repaired == raw_text:
            return None, raw_text, _structured_error(first_error)
        try:
            return parse_structured_text(repaired, model_cls), repaired, None
        except StructuredOutputParseError as second_error:
            return None, raw_text, _structured_error(second_error)


# ---------------------------------------------------------------------------
# Fake client (offline)
# ---------------------------------------------------------------------------


class _StreamInterrupted:
    """Sentinel kịch bản stream: phát chunk cuối `partial` rồi dừng."""

    def __repr__(self) -> str:
        return "STREAM_INTERRUPTED"


#: Dùng trong `FakeLLMClient.queue_stream(...)` để mô phỏng stream đứt.
STREAM_INTERRUPTED = _StreamInterrupted()


class FakeLLMClient:
    """Fake LLM client offline có kịch bản, dùng cho test deterministic.

    `responses` là hàng đợi theo thứ tự gọi `complete`: `str` trả về text,
    `Exception` raise tại lượt đó. Hết hàng đợi thì raise `LLMError`, nên test
    không bao giờ vô tình gọi mạng và cũng không "pass" nhờ response ngầm định.

    Stream dùng `queue_stream(*chunks)`:

    - `str` -> một chunk `delta`;
    - `StreamChunk` -> phát nguyên trạng (chunk không phải `delta` kết thúc stream);
    - `STREAM_INTERRUPTED` -> phát chunk cuối `partial` và dừng (**không**
      `completed`), mô phỏng stream đứt;
    - `Exception` -> raise tại lượt đó.

    Không script stream thì client lấy một item từ hàng đợi `responses` và phát
    nó như một delta + `completed`. Mọi lượt gọi đều được ghi vào `calls`.
    """

    def __init__(
        self,
        responses: Iterable[str | Exception] | str | Exception | None = None,
        *,
        model: str | None = "fake-model",
    ) -> None:
        if responses is None:
            queued: list[str | Exception] = []
        elif isinstance(responses, (str, Exception)):
            queued = [responses]
        else:
            queued = list(responses)
        self._queue = queued
        self._stream_queue: list[Any] = []
        self.model = model
        self.calls: list[LLMRequest] = []

    def queue(self, *responses: str | Exception) -> None:
        """Thêm response `complete` vào cuối hàng đợi."""
        self._queue.extend(responses)

    def queue_stream(self, *chunks: Any) -> None:
        """Thêm kịch bản stream.

        Nhận nhiều đối số hoặc một list/tuple duy nhất:
        `queue_stream("a", "b", STREAM_INTERRUPTED)`.
        """
        if len(chunks) == 1 and isinstance(chunks[0], (list, tuple)):
            chunks = tuple(chunks[0])
        if not chunks:
            raise ValueError("queue_stream cần ít nhất một chunk.")
        self._stream_queue.extend(chunks)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._queue:
            raise LLMError(
                "FakeLLMClient đã hết response được script; hãy queue trước khi gọi."
            )
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, model=self.model, finish_reason="stop")

    def stream(self, request: LLMRequest) -> Iterator[StreamChunk]:
        self.calls.append(request)
        if self._stream_queue:
            script = list(self._stream_queue)
            self._stream_queue.clear()
        elif self._queue:
            script = [self._queue.pop(0)]
        else:
            raise LLMError(
                "FakeLLMClient đã hết response/stream được script; hãy queue trước khi gọi."
            )

        terminal = False
        for item in script:
            if isinstance(item, Exception):
                raise item
            if item is STREAM_INTERRUPTED:
                terminal = True
                yield self._partial_chunk()
                return
            if isinstance(item, StreamChunk):
                yield item
                if item.status != "delta":
                    return
                continue
            if isinstance(item, str):
                if item:
                    yield StreamChunk(text=item, status="delta")
                continue
            raise LLMError(
                f"FakeLLMClient: chunk stream không hỗ trợ {type(item).__name__}; "
                "dùng str, StreamChunk, STREAM_INTERRUPTED hoặc Exception."
            )
        if not terminal:
            yield StreamChunk(
                text="",
                status="completed",
                finish_reason="stop",
                raw={"model": self.model},
            )

    @staticmethod
    def _partial_chunk() -> StreamChunk:
        return StreamChunk(
            text="",
            status="partial",
            finish_reason="interrupted",
            raw={"reason": "stream_interrupted"},
        )

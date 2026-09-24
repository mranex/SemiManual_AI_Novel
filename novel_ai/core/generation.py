"""Event generation dùng chung (T33; contract ở `schemas.md` mục 11.1–11.2, D019).

Module này **thuần Python**: không import Streamlit, không giữ state bền và không
gọi mạng. Service/adapter phát `GenerationEvent`; UI (page hoặc
React WebUI hiển thị qua HTTP SSE; event không phụ thuộc transport.

Luật quan trọng nhất của contract:

- `transport_complete` (đã nhận xong response) **không** phải "xong". Chỉ `saved`
  mới là candidate/draft đã parse + validate + ghi bền, và chỉ `saved` mới cho UI
  báo "candidate ready" hoặc mở Review/Finalize.
- `partial`, `invalid`, `error` là terminal và **không** tạo candidate complete,
  không auto accept, không đổi accepted state.
- `text_delta` chỉ là raw preview tăng dần; nó không phải payload đã validate.
- Event không chứa API key, full prompt, author-only context hay future plot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

__all__ = [
    "CANDIDATE_READY_STATES",
    "GENERATION_STATES",
    "GenerationEmitter",
    "GenerationEvent",
    "GenerationStateError",
    "GenerationTranscript",
    "TERMINAL_STATES",
    "TRANSPORT_KINDS",
    "allowed_transitions",
    "can_transition",
]

#: Toàn bộ state của state machine (schemas.md mục 11.2).
GENERATION_STATES: tuple[str, ...] = (
    "idle",
    "connecting",
    "non_streaming",
    "streaming",
    "transport_complete",
    "validating",
    "saved",
    "partial",
    "invalid",
    "error",
)

#: Terminal state: không có chuyển tiếp nào đi tiếp.
TERMINAL_STATES: tuple[str, ...] = ("saved", "partial", "invalid", "error")

#: Chỉ state này cho phép UI coi candidate/draft đã sẵn sàng.
CANDIDATE_READY_STATES: tuple[str, ...] = ("saved",)

#: Cách transport thật sự đã chạy (không được giả stream).
TRANSPORT_KINDS: tuple[str, ...] = ("streaming", "non_streaming")

_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("connecting", "saved"),
    "connecting": ("streaming", "non_streaming", "error"),
    "non_streaming": ("transport_complete", "error"),
    "streaming": ("streaming", "transport_complete", "partial", "error"),
    "transport_complete": ("validating",),
    # `validating` có bốn kết cục: `saved` (candidate/draft hoàn chỉnh đã ghi),
    # `partial` (chỉ giữ được bản dở đã lưu), `invalid` (không có gì dùng được) và
    # `error` (lỗi ghi/parse bất ngờ trong bước này).
    "validating": ("saved", "partial", "invalid", "error"),
    "saved": (),
    "partial": (),
    "invalid": (),
    "error": (),
}


class GenerationStateError(ValueError):
    """Chuyển tiếp state không hợp lệ theo contract generation."""


def allowed_transitions(status: str) -> tuple[str, ...]:
    """Các state có thể đi tiếp từ `status` (state lạ ⇒ không có)."""
    return tuple(_TRANSITIONS.get(status, ()))


def can_transition(current: str, target: str) -> bool:
    """True nếu `current -> target` hợp lệ (self-transition của `streaming` hợp lệ)."""
    if current == target and current == "streaming":
        return True
    return target in allowed_transitions(current)


@dataclass(frozen=True)
class GenerationEvent:
    """Một event trong vòng đời một lần generation (schemas.md mục 11.1)."""

    status: str
    operation_id: str
    action: str
    attempt: int = 1
    transport: str = "non_streaming"
    prompt_id: str | None = None
    artifact_id: str | None = None
    chapter_id: str | None = None
    text_delta: str = ""
    detail: str = ""
    raw_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in GENERATION_STATES:
            raise GenerationStateError(f"status `{self.status}` không thuộc contract generation.")
        if self.transport not in TRANSPORT_KINDS:
            raise GenerationStateError(f"transport `{self.transport}` không hợp lệ.")
        if int(self.attempt) < 1:
            raise GenerationStateError("attempt phải >= 1.")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def candidate_ready(self) -> bool:
        """Chỉ `saved` mới được coi là candidate/draft đã validate và lưu."""
        return self.status in CANDIDATE_READY_STATES

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation_id": self.operation_id,
            "action": self.action,
            "attempt": int(self.attempt),
            "transport": self.transport,
            "prompt_id": self.prompt_id,
            "artifact_id": self.artifact_id,
            "chapter_id": self.chapter_id,
            "text_delta": self.text_delta,
            "detail": self.detail,
            "raw_ref": self.raw_ref,
        }


#: Callback nhận event. Trả `None`; service không quan tâm UI làm gì với nó.
EventSink = Callable[[GenerationEvent], None]


class GenerationEmitter:
    """Phát event theo đúng thứ tự state machine cho **một** lần generation.

    Service dùng class này thay vì tự ghép event: mọi chuyển tiếp đều được kiểm, và
    `text_delta` chỉ được gắn khi transport thật sự đang stream.
    """

    def __init__(
        self,
        *,
        operation_id: str,
        action: str,
        sink: EventSink | None = None,
        attempt: int = 1,
        prompt_id: str | None = None,
        artifact_id: str | None = None,
        chapter_id: str | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.action = action
        self.attempt = max(1, int(attempt))
        self.prompt_id = prompt_id
        self.artifact_id = artifact_id
        self.chapter_id = chapter_id
        self._sink = sink
        self._current = "idle"
        self._transport = "non_streaming"

    @property
    def current(self) -> str:
        return self._current

    @property
    def transport(self) -> str:
        return self._transport

    def emit(
        self,
        status: str,
        *,
        text_delta: str = "",
        detail: str = "",
        raw_ref: str | None = None,
        transport: str | None = None,
    ) -> GenerationEvent:
        """Phát một event và cập nhật state hiện tại; raise nếu chuyển tiếp sai."""
        if transport is not None:
            self._transport = transport
        if not can_transition(self._current, status):
            raise GenerationStateError(
                f"Chuyển tiếp generation không hợp lệ: `{self._current}` → `{status}` "
                f"(cho phép: {allowed_transitions(self._current) or 'không có'})."
            )
        event = GenerationEvent(
            status=status,
            operation_id=self.operation_id,
            action=self.action,
            attempt=self.attempt,
            transport=self._transport,
            prompt_id=self.prompt_id,
            artifact_id=self.artifact_id,
            chapter_id=self.chapter_id,
            text_delta=text_delta,
            detail=detail,
            raw_ref=raw_ref,
        )
        self._current = status
        if self._sink is not None:
            self._sink(event)
        return event

    # -- helper theo từng bước -------------------------------------------------

    def start_streaming(self, *, detail: str = "") -> GenerationEvent:
        """Phát `connecting` rồi `streaming`.

        Transport được chốt **trước** `connecting` để mọi event của lần chạy này
        đều mang đúng `transport="streaming"`. Trả event **cuối**; caller nối vào
        `sink` sẽ nhận đủ cả hai event theo thứ tự. Transcript chỉ nên `apply` các
        event đã đi qua sink.
        """
        self._transport = "streaming"
        self.emit("connecting", detail=detail)
        return self.emit("streaming", transport="streaming", detail=detail)

    def start_non_streaming(self, *, detail: str = "") -> GenerationEvent:
        """Phát `connecting` rồi `non_streaming` (trả event cuối)."""
        self.emit("connecting", detail=detail)
        return self.emit("non_streaming", transport="non_streaming", detail=detail)

    def delta(self, text: str) -> GenerationEvent:
        """Delta chỉ hợp lệ khi transport thật sự đang `streaming`."""
        if self._current != "streaming" or self._transport != "streaming":
            raise GenerationStateError(
                "Chỉ được phát delta khi transport đang streaming (không giả stream)."
            )
        return self.emit("streaming", text_delta=str(text))

    def transport_complete(self, *, detail: str = "") -> GenerationEvent:
        return self.emit("transport_complete", detail=detail)

    def validating(self, *, detail: str = "") -> GenerationEvent:
        return self.emit("validating", detail=detail)

    def replay(self, *, detail: str = "Operation đã hoàn tất trước đó; replay theo operation_id.") -> GenerationEvent:
        """Operation đã chạy thành công (replay theo `operation_id`): vào thẳng `saved`.

        Không gọi LLM và không ghi thêm gì; event chỉ để UI không hiển thị như
        "đang chạy".
        """
        return self.emit("saved", detail=detail)

    def saved(self, *, detail: str = "", raw_ref: str | None = None) -> GenerationEvent:
        return self.emit("saved", detail=detail, raw_ref=raw_ref)

    def partial(self, *, detail: str = "", raw_ref: str | None = None) -> GenerationEvent:
        return self.emit("partial", detail=detail, raw_ref=raw_ref)

    def invalid(self, *, detail: str = "", raw_ref: str | None = None) -> GenerationEvent:
        return self.emit("invalid", detail=detail, raw_ref=raw_ref)

    def fail(self, *, detail: str = "") -> GenerationEvent:
        return self.emit("error", detail=detail)


#: Số ký tự raw preview giữ trong transcript (đủ để phục hồi bằng mắt, không phình state).
MAX_TRANSCRIPT_CHARS = 20_000


@dataclass
class GenerationTranscript:
    """Bản ghi một lần generation trong **phiên** (UI giữ trong `st.session_state`).

    Chỉ chứa raw text preview + trạng thái + `raw_ref`. Đây không phải state bền:
    raw/partial quan trọng đã được service lưu qua storage, và UI hiển thị lại
    thông tin phục hồi từ disk khi transcript không còn (mở lại app/project khác).
    """

    operation_id: str
    action: str
    attempt: int = 1
    transport: str = "non_streaming"
    state: str = "idle"
    text: str = ""
    detail: str = ""
    raw_ref: str | None = None
    prompt_id: str | None = None
    artifact_id: str | None = None
    chapter_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def candidate_ready(self) -> bool:
        return self.state in CANDIDATE_READY_STATES

    @property
    def running(self) -> bool:
        return self.state in {"connecting", "streaming", "non_streaming", "transport_complete", "validating"}

    def apply(self, event: GenerationEvent) -> None:
        """Áp event vào transcript; raise nếu chuyển tiếp sai contract."""
        if event.operation_id != self.operation_id:
            raise GenerationStateError(
                "Event thuộc operation khác: transcript được scope theo operation_id."
            )
        if not can_transition(self.state, event.status):
            raise GenerationStateError(
                f"Transcript không nhận `{self.state}` → `{event.status}`."
            )
        if event.text_delta:
            self.text = (self.text + event.text_delta)[-MAX_TRANSCRIPT_CHARS:]
        self.state = event.status
        self.transport = event.transport
        self.attempt = event.attempt
        if event.detail:
            self.detail = event.detail
        if event.raw_ref:
            self.raw_ref = event.raw_ref
        if event.prompt_id:
            self.prompt_id = event.prompt_id
        if event.artifact_id:
            self.artifact_id = event.artifact_id
        if event.chapter_id:
            self.chapter_id = event.chapter_id
        self.events.append(event.as_dict())
        # Giữ transcript gọn: chỉ cần vài event gần nhất để hiển thị/diễn giải.
        del self.events[:-20]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "attempt": int(self.attempt),
            "transport": self.transport,
            "state": self.state,
            "text": self.text,
            "detail": self.detail,
            "raw_ref": self.raw_ref,
            "prompt_id": self.prompt_id,
            "artifact_id": self.artifact_id,
            "chapter_id": self.chapter_id,
            "events": list(self.events),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GenerationTranscript:
        return cls(
            operation_id=str(data.get("operation_id", "")),
            action=str(data.get("action", "")),
            attempt=int(data.get("attempt", 1) or 1),
            transport=str(data.get("transport", "non_streaming")),
            state=str(data.get("state", "idle")),
            text=str(data.get("text", "")),
            detail=str(data.get("detail", "")),
            raw_ref=data.get("raw_ref"),
            prompt_id=data.get("prompt_id"),
            artifact_id=data.get("artifact_id"),
            chapter_id=data.get("chapter_id"),
            events=[dict(item) for item in data.get("events", []) or []],
        )

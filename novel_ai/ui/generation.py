"""Generation surface dùng chung (T33; contract `schemas.md` mục 11, D019).

Module này là **UI**: nó chỉ render primitive Streamlit và giữ transcript trong
`st.session_state`. Service không import module này (xem `novel_ai/core/generation.py`).

Luật đã giữ:

- transcript được scope theo `project_id` + `workspace` + `artifact_id`, nên đổi
  workspace/project không hiển thị nhầm response cũ như candidate mới;
- transcript sống qua rerun trong phiên, nhưng **không** phải state bền: raw/partial
  quan trọng đã được service lưu qua storage, và surface đọc lại thông tin phục hồi
  từ disk (`writer.latest_operation_for`/`raw_output_ref`) khi transcript không còn;
- chỉ trạng thái `saved` mới hiển thị "đã lưu/validate"; `partial`/`invalid`/`error`
  hiển thị là **chưa** dùng được, không mở Review/Finalize;
- provider non-streaming được ghi rõ, không giả token stream;
- raw preview chỉ là phần text model trả về, giới hạn độ dài; không in prompt,
  API key hay context secret.
"""

from __future__ import annotations

from typing import Any, Mapping

from novel_ai.core.generation import GenerationEvent, GenerationTranscript

__all__ = [
    "KEY_TRANSCRIPT",
    "clear_transcript",
    "make_recorder",
    "recorder_for",
    "render_disk_recovery_note",
    "render_generation_surface",
    "scope_key",
    "start_surface",
    "surface_labels",
]

#: `st.session_state` key giữ `scoped_key -> transcript dict`.
KEY_TRANSCRIPT = "novel_ai_generation_transcripts"

#: Số ký tự preview hiển thị trong surface.
PREVIEW_CHARS = 1_200


def scope_key(*, project_id: str, workspace: str, artifact_id: str) -> str:
    """Khóa transcript: theo project + workspace + artifact (D019 điểm 6)."""
    return f"{project_id}|{workspace}|{artifact_id}"


def _transcripts() -> dict[str, Any]:
    import streamlit as st

    store = st.session_state.get(KEY_TRANSCRIPT)
    if not isinstance(store, dict):
        store = {}
        st.session_state[KEY_TRANSCRIPT] = store
    return store


def load_transcript(scoped_key: str) -> GenerationTranscript | None:
    """Transcript của scope này trong phiên, hoặc `None` nếu chưa có."""
    data = _transcripts().get(scoped_key)
    if not isinstance(data, Mapping):
        return None
    return GenerationTranscript.from_dict(data)


def save_transcript(scoped_key: str, transcript: GenerationTranscript) -> None:
    _transcripts()[scoped_key] = transcript.as_dict()


def clear_transcript(scoped_key: str) -> None:
    """Xóa transcript của một scope (dùng khi bắt đầu operation mới ở scope đó)."""
    _transcripts().pop(scoped_key, None)


def surface_labels() -> dict[str, str]:
    """Nhãn tiếng Việt cho từng state (một nguồn duy nhất cho mọi page)."""
    return {
        "idle": "chưa chạy",
        "connecting": "đang gửi request",
        "non_streaming": "provider chạy non-streaming",
        "streaming": "đang nhận delta",
        "transport_complete": "đã nhận xong response (chưa validate)",
        "validating": "đang parse/validate",
        "saved": "đã validate và lưu",
        "partial": "partial — chưa dùng được",
        "invalid": "output không hợp lệ",
        "error": "lỗi trước khi có dữ liệu",
    }


class GenerationSurface:
    """Vùng hiển thị ở đầu workspace, cập nhật tại chỗ trong lúc action chạy."""

    def __init__(
        self, scoped_key: str, placeholder: Any, *, disk_recovery: bool = False
    ) -> None:
        self.scoped_key = scoped_key
        self.placeholder = placeholder
        #: Chỉ bật cho surface mà `latest_operation_for(chapter)` thuộc **cùng**
        #: action (ví dụ Writer), tránh hiển thị operation của workspace khác như
        #: thông tin phục hồi của surface này.
        self.disk_recovery = disk_recovery

    def render(
        self,
        transcript: GenerationTranscript | None,
        *,
        project: Any | None = None,
        chapter_id: str | None = None,
    ) -> None:
        with self.placeholder.container():
            render_generation_surface(
                transcript,
                project=project,
                chapter_id=chapter_id,
                disk_recovery=self.disk_recovery,
            )


def start_surface(
    scoped_key: str,
    *,
    transcript: GenerationTranscript | None,
    project: Any | None = None,
    chapter_id: str | None = None,
    title: str = "AI Generation",
    disk_recovery: bool = False,
) -> GenerationSurface:
    """Tạo surface ở đầu workspace và render transcript hiện có.

    Trả object giữ `st.empty()` để recorder cập nhật **tại chỗ** trong lúc stream.
    `disk_recovery=True` chỉ dùng cho surface mà operation gần nhất trên disk đúng
    là action của surface đó (Writer); mặc định tắt để không hiển thị operation của
    workspace khác.
    """
    import streamlit as st

    st.markdown(f"**{title}**")
    placeholder = st.empty()
    surface = GenerationSurface(scoped_key, placeholder, disk_recovery=disk_recovery)
    surface.render(transcript, project=project, chapter_id=chapter_id)
    return surface


def make_recorder(
    surface: GenerationSurface,
    transcript: GenerationTranscript,
    *,
    project: Any | None = None,
    chapter_id: str | None = None,
) -> Any:
    """`on_event` callback: áp event vào transcript rồi render lại surface.

    Transcript được ghi vào session state sau **mỗi** event, nên nó sống qua rerun
    và không phụ thuộc việc action có kịp `set_action_result` hay không.
    """

    def _on_event(event: GenerationEvent) -> None:
        transcript.apply(event)
        save_transcript(surface.scoped_key, transcript)
        surface.render(transcript, project=project, chapter_id=chapter_id)

    return _on_event


def recorder_for(
    surface: GenerationSurface,
    *,
    action: str,
    operation_id: str,
    stream: bool,
    attempt: int = 1,
    artifact_id: str | None = None,
    chapter_id: str | None = None,
    prompt_id: str | None = None,
    project: Any | None = None,
) -> tuple[GenerationTranscript, Any]:
    """Transcript của scope + `on_event` gắn vào surface.

    Transcript được **tái sử dụng** khi cùng `operation_id` (replay/rerun) và tạo
    mới khi operation đổi, nên response cũ không lẫn vào action mới. Page gọi hàm
    này ngay trước khi gọi service rồi truyền `on_event` xuống service.
    """
    existing = load_transcript(surface.scoped_key)
    if existing is None or existing.operation_id != operation_id:
        existing = GenerationTranscript(
            operation_id=operation_id,
            action=action,
            attempt=int(attempt),
            artifact_id=artifact_id,
            chapter_id=chapter_id,
            prompt_id=prompt_id,
            transport="streaming" if stream else "non_streaming",
        )
        save_transcript(surface.scoped_key, existing)
    recorder = make_recorder(
        surface, existing, project=project, chapter_id=chapter_id
    )
    return existing, recorder


def render_disk_recovery_note(
    *, project: Any | None, chapter_id: str | None, container: Any | None = None
) -> None:
    """Thông tin phục hồi đọc từ **disk** (không phải session state).

    Dùng khi transcript không còn (mở lại app/project): người dùng vẫn thấy run gần
    nhất, raw output và trạng thái complete/partial thật đã ghi.
    """
    import streamlit as st

    target = container if container is not None else st
    if project is None or not chapter_id:
        return
    from novel_ai.services import writer

    record = writer.latest_operation_for(project, chapter_id)
    if not record:
        target.caption("Chưa có operation generation nào trên disk cho chương này.")
        return
    complete = bool(record.get("is_complete"))
    target.caption(
        "Run gần nhất trên disk: "
        f"`{record.get('operation_type')}` · op `{record.get('operation_id')}` · "
        f"revision {record.get('revision')} · complete `{complete}` · "
        f"stream `{record.get('stream_status')}`"
        + (f" · lý do `{record.get('reason')}`" if record.get("reason") else "")
    )
    raw_ref = record.get("raw_output_ref")
    if raw_ref:
        target.caption(f"Raw/partial output để phục hồi: `{raw_ref}`")


def render_generation_surface(
    transcript: GenerationTranscript | None,
    *,
    project: Any | None = None,
    chapter_id: str | None = None,
    disk_recovery: bool = False,
) -> None:
    """Render state + raw preview + việc cần làm tiếp của một lần generation."""
    import streamlit as st

    labels = surface_labels()
    if transcript is None:
        st.caption(
            "Chưa có generation nào trong phiên này ở workspace đang mở. Rerun/toggle "
            "không tự gọi API."
        )
        if disk_recovery:
            render_disk_recovery_note(project=project, chapter_id=chapter_id)
        return

    state = transcript.state
    st.markdown(
        f"State: **{labels.get(state, state)}** · action `{transcript.action}` · "
        f"op `{transcript.operation_id}` · lần chạy {transcript.attempt} · "
        f"transport `{transcript.transport}`"
    )
    if transcript.detail:
        st.caption(transcript.detail)
    if transcript.transport == "non_streaming" and transcript.running:
        st.info(
            "Provider không stream cho action này: không có delta để hiển thị, UI chỉ "
            "báo trạng thái thật (không giả token stream)."
        )

    if transcript.text:
        st.caption(
            f"Raw preview ({len(transcript.text)} ký tự, chỉ là output thô chưa validate):"
        )
        st.code(transcript.text[-PREVIEW_CHARS:])

    if state == "saved":
        st.success("Đã validate và lưu. Candidate/draft sẵn sàng cho bước tiếp theo.")
    elif state == "partial":
        st.warning(
            "Partial: bản dở đã được giữ để phục hồi, **không** mở Review/Finalize và "
            "không auto accept. Dùng Continue hoặc chạy lại bằng action tường minh."
        )
        if transcript.raw_ref:
            st.caption(f"Raw partial: `{transcript.raw_ref}`")
    elif state == "invalid":
        st.error(
            "Output không hợp lệ (rỗng hoặc chỉ là thông báo lỗi): không tạo candidate, "
            "accepted/draft cũ giữ nguyên."
        )
        if transcript.raw_ref:
            st.caption(f"Raw output để kiểm tra: `{transcript.raw_ref}`")
    elif state == "error":
        st.error(
            "Lỗi trước khi có dữ liệu dùng được: không có gì được ghi và state cũ giữ nguyên. "
            "Retry chỉ chạy khi bạn bấm lại (không tự gửi request thứ hai sau partial)."
        )
    elif transcript.running:
        st.caption("Đang chạy… (stream thật: delta hiện dần ở trên)")

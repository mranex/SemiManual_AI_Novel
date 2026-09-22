"""Workspace Writer: gate, generate/stream, save/continue/regenerate/discard (T21).

Page này hiện thực `docs/design/workflow.md` mục 3, 4.3, 5.1, 5.5 và D011/D015:

- **Gate hiển thị trước khi Generate**: `lifecycle.guard_writer` (Skeleton
  accepted/fresh, chương N>1 cần N-1 `final_reconciled`, state chain không stale)
  cộng blocker/bước tiếp theo từ `novel_ai.ui.arbiter.analyze`. Arbiter chỉ gợi ý;
  backend mới là nơi chặn.
- **Stream**: nếu client hỗ trợ `stream`, UI hiện tiến độ theo từng delta và nói rõ
  stream đứt ⇒ `partial`.
- **Partial khác draft hoàn chỉnh**: partial không mở review/finalize; UI nói rõ và
  không hiện đường Finalize.
- **Mutation** (`writer.generate_draft`, `regenerate_draft`, `continue_draft`,
  `save_draft`, `discard_draft`) nằm sau `st.button`; rerun thuần không gọi LLM và
  không ghi file. `operation_id` tất định theo input nên bấm lặp là replay.
"""

from __future__ import annotations

from typing import Any

from novel_ai.core.generation import GenerationTranscript
from novel_ai.services import ServiceError, writer
from novel_ai.ui import generation, page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "ACTIONS",
    "KEY_ACTION",
    "KEY_ATTEMPT",
    "KEY_CHAPTER",
    "KEY_DISCARD",
    "KEY_DISCARD_REVISION",
    "KEY_INSTRUCTION",
    "KEY_RELOAD_PROSE",
    "KEY_RUN",
    "KEY_SAVE",
    "KEY_STREAM",
    "render",
]

#: Action Writer gọi LLM; `continue` dùng instruction cố định của service.
ACTIONS: tuple[str, ...] = ("generate", "regenerate", "continue")

KEY_CHAPTER = "novel_ai_writer_chapter"
KEY_ACTION = "novel_ai_writer_action"
KEY_INSTRUCTION = "novel_ai_writer_instruction"
KEY_STREAM = "novel_ai_writer_stream"
KEY_ATTEMPT = "novel_ai_writer_attempt"
KEY_RUN = "novel_ai_writer_run"
KEY_SAVE = "novel_ai_writer_save"
KEY_RELOAD_PROSE = "novel_ai_writer_reload_prose"
KEY_DISCARD_REVISION = "novel_ai_writer_discard_revision"
KEY_DISCARD = "novel_ai_writer_discard"


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Writer",
        "Writer chỉ sinh prose draft từ Skeleton accepted; không lập plan và không sửa state.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Writer.")
        return

    rows = _chapter_ui.chapter_rows(project)
    if not rows:
        st.info("Chưa có chapter nào. Hoàn tất Short Plan accepted trước.")
        return

    chapter_id = _chapter_ui.chapter_select(KEY_CHAPTER, rows)
    if chapter_id is None:  # pragma: no cover
        return

    # Generation surface ở đầu workspace (T33, UI-02): đọc transcript đã scope theo
    # project/workspace/artifact; nếu chưa có thì hiển thị thông tin phục hồi từ disk.
    scoped = generation.scope_key(
        project_id=project.config.project_id, workspace="writer", artifact_id=chapter_id
    )
    transcript = generation.load_transcript(scoped)
    surface = generation.start_surface(
        scoped,
        transcript=transcript,
        project=project,
        chapter_id=chapter_id,
        # Writer là surface mà operation gần nhất trên disk đúng là của nó, nên
        # thông tin phục hồi từ disk thuộc cùng action.
        disk_recovery=True,
    )

    _render_gate_panel(project, chapter_id)
    _render_draft_panel(ctx, chapter_id)
    _render_run_panel(ctx, chapter_id, surface=surface, transcript=transcript)
    _render_save_panel(project, chapter_id)
    _render_discard_panel(project, chapter_id)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _render_gate_panel(project: Any, chapter_id: str) -> None:
    """Gate Writer lấy từ backend guard + gợi ý Arbiter (không tự suy luận luật)."""
    import streamlit as st

    st.subheader("Gate trước khi Generate")
    gate = _chapter_ui.writer_gate(project, chapter_id)
    if gate.allowed:
        st.success("Backend cho phép Writer chạy cho chương này.")
    else:
        st.error(
            f"Backend **đang chặn** Writer (code `{gate.code}`). Nút bên dưới vẫn bấm "
            "được, nhưng service sẽ từ chối và không gọi LLM (D011)."
        )
    for reason in gate.reasons:
        st.markdown(f"- {reason}")
    st.caption("Gợi ý Arbiter (chỉ gợi ý, bạn quyết định): " + gate.arbiter_next)
    if gate.arbiter_blockers:
        st.markdown("**Blocker liên quan tới chương này** (từ Arbiter):")
        for item in gate.arbiter_blockers:
            st.markdown(f"- {item}")


def _render_draft_panel(ctx: AppContext, chapter_id: str) -> None:
    """State draft hiện tại + operation record gần nhất (phân biệt partial/complete)."""
    import streamlit as st

    project = ctx.project
    assert project is not None
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    st.subheader("Draft hiện tại")
    if chapter is None:  # pragma: no cover
        return
    st.caption(_chapter_ui.chapter_status_note(chapter.status.value))
    draft = chapter.current_draft
    if draft is None:
        st.caption("Chương này chưa có prose revision nào.")
    else:
        kind = "complete" if draft.is_complete else "**partial**"
        st.markdown(
            f"Prose hiện tại: **r{draft.revision}** · {kind} · source "
            f"`{draft.source_type.value}` · tạo lúc {draft.created_at}"
        )
        if draft.is_complete:
            st.caption("Draft complete: có thể chạy AI Review/Human Review rồi Finalize.")
        else:
            st.warning(_chapter_ui.PARTIAL_NOTE)
        pins = _chapter_ui.pin_lines(draft.dependency_pins)
        if pins:
            st.caption("Pin của draft: " + " · ".join(pins))

    operations = writer.latest_operation_for(project, chapter_id)
    if operations:
        st.caption(
            "Operation record gần nhất (đọc qua `writer.latest_operation_for`; là metadata "
            "rerun/retry, không phải canon):"
        )
        st.markdown(
            f"- `{operations.get('operation_type')}` · op `{operations.get('operation_id')}` · "
            f"revision {operations.get('revision')} · complete `{operations.get('is_complete')}` · "
            f"stream `{operations.get('stream_status')}`"
            + (f" · lý do `{operations.get('reason')}`" if operations.get("reason") else "")
        )


# ---------------------------------------------------------------------------
# Generate / regenerate / continue + stream
# ---------------------------------------------------------------------------


def _render_run_panel(
    ctx: AppContext,
    chapter_id: str,
    *,
    surface: generation.GenerationSurface,
    transcript: GenerationTranscript | None,
) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Generate / Regenerate / Continue")
    action = st.selectbox("Action", ACTIONS, key=KEY_ACTION)
    instruction = ""
    if action != "continue":
        instruction = st.text_area(
            "Yêu cầu thêm cho lần chạy (tuỳ chọn)", key=KEY_INSTRUCTION, height=80
        )
    else:
        st.caption(
            "Continue nối prose vào tail của draft hiện tại, cùng Skeleton/context; "
            "không tạo plan mới và không đổi upstream."
        )
    can_stream = _chapter_ui.stream_supported(ctx.llm_client)
    stream = st.checkbox(
        "Stream (hiện tiến độ theo từng delta)",
        value=bool(can_stream),
        key=KEY_STREAM,
        disabled=not can_stream,
        help="Client hiện tại không có `stream` thì tuỳ chọn này bị khóa." if not can_stream else "",
    )
    attempt = _chapter_ui.attempt_input(KEY_ATTEMPT)

    if ctx.llm_client is None or ctx.registry is None:
        st.error(
            "Chưa dùng được action gọi LLM: "
            + ("thiếu prompt registry. " if ctx.registry is None else "")
            + ("chưa dựng được LLM client. " if ctx.llm_client is None else "")
            + (ctx.llm_error or "")
        )
        return

    if not st.button("Chạy action Writer", key=KEY_RUN):
        return

    intent = (chapter_id, str(action), str(instruction or "").strip(), bool(stream), int(attempt))
    operation_id = _chapter_ui.stable_operation_id("writer", *intent)
    # Operation mới ⇒ transcript mới cho scope này. Replay cùng operation_id vẫn
    # dùng lại transcript cũ (không có request thứ hai).
    if transcript is None or transcript.operation_id != operation_id:
        transcript = GenerationTranscript(
            operation_id=operation_id,
            action=str(action),
            attempt=int(attempt),
            artifact_id=chapter_id,
            chapter_id=chapter_id,
            transport="streaming" if stream else "non_streaming",
            prompt_id=writer.DRAFT_PROMPT_ID,
        )
    on_event = generation.make_recorder(
        surface, transcript, project=project, chapter_id=chapter_id
    )

    kwargs: dict[str, Any] = {
        "client": ctx.llm_client,
        "chapter_id": chapter_id,
        "stream": bool(stream),
        "on_event": on_event,
        "attempt": int(attempt),
        "operation_id": operation_id,
    }

    def _call() -> Any:
        if action == "generate":
            return writer.generate_draft(
                project, user_instruction=str(instruction or "").strip(), **kwargs
            )
        if action == "regenerate":
            return writer.regenerate_draft(
                project, user_instruction=str(instruction or "").strip(), **kwargs
            )
        return writer.continue_draft(project, **kwargs)

    try:
        blocked, result = _chapter_ui.run_action(
            KEY_RUN, intent=intent, project=project, call=_call
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu là guard: xử lý Skeleton/chương trước trước (xem panel Gate). Nếu là "
                "lỗi provider: draft cũ giữ nguyên, thử lại hoặc Continue sau."
            ),
        )
        surface.render(transcript, project=project, chapter_id=chapter_id)
        return
    if blocked:
        surface.render(transcript, project=project, chapter_id=chapter_id)
        return
    surface.render(transcript, project=project, chapter_id=chapter_id)
    set_action_result(result)
    st.rerun()


# ---------------------------------------------------------------------------
# Save / discard
# ---------------------------------------------------------------------------


def _render_save_panel(project: Any, chapter_id: str) -> None:
    import streamlit as st

    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    st.subheader("Save Draft (sửa prose)")
    if chapter is None:  # pragma: no cover
        return
    draft = chapter.current_draft
    if draft is None:
        st.info("Chưa có prose revision để sửa. Chạy Generate trước.")
        return
    text = _chapter_ui.prose_text(project, chapter_id, draft.revision)
    key = f"novel_ai_writer_prose_{chapter_id}"
    version = (chapter_id, draft.revision, _chapter_ui.payload_fingerprint(text))
    if st.button("Nạp lại editor từ prose hiện tại", key=KEY_RELOAD_PROSE):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    st.caption(
        f"Prose revision hiện tại: **r{draft.revision}** · "
        f"{'complete' if draft.is_complete else 'partial'} · source `{draft.source_type.value}`."
    )
    edited = _common.render_prose_editor(
        key,
        text,
        version=version,
        label="Prose (markdown). Save tạo prose revision **mới** và làm Human Review cũ mất hiệu lực.",
    )
    st.caption(
        "Save Draft tạo revision mới thay vì sửa revision cũ: accepted/final không bị "
        "silent overwrite (workflow.md mục 4.3)."
    )
    if not st.button("Save Draft", key=KEY_SAVE):
        return
    intent = (chapter_id, _chapter_ui.payload_fingerprint(edited))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_SAVE,
            intent=intent,
            project=project,
            call=lambda: writer.save_draft(
                project,
                chapter_id=chapter_id,
                text=edited,
                operation_id=_chapter_ui.stable_operation_id("writer", "save", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Sửa nội dung rồi lưu lại. Nếu guard báo chapter đã final/finalizing, "
                "dùng workspace Revision (retcon) hoặc workspace Reconcile."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(key, f"{key}__source")
    st.rerun()


def _render_discard_panel(project: Any, chapter_id: str) -> None:
    import streamlit as st

    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    st.subheader("Discard prose revision cũ")
    if chapter is None:  # pragma: no cover
        return
    others = [
        draft for draft in chapter.drafts if draft.revision != chapter.current_draft_revision
    ]
    if not others:
        st.caption(
            "Không có revision cũ nào để bỏ. Storage không cho lùi `current_draft_revision`, "
            "nên muốn thay bản đang hiển thị hãy Save Draft/Regenerate rồi bỏ bản cũ."
        )
        return
    options = [draft.revision for draft in others]
    labels = {
        draft.revision: f"r{draft.revision} · {'complete' if draft.is_complete else 'partial'} · "
        f"{draft.created_at}"
        for draft in others
    }
    chosen = st.selectbox(
        "Revision muốn bỏ",
        options,
        format_func=lambda value: labels.get(value, str(value)),
        key=KEY_DISCARD_REVISION,
    )
    st.caption("Discard chỉ bỏ metadata revision; file markdown được giữ để audit.")
    if not st.button("Discard revision", key=KEY_DISCARD):
        return
    intent = (chapter_id, int(chosen))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_DISCARD,
            intent=intent,
            project=project,
            call=lambda: writer.discard_draft(
                project,
                chapter_id=chapter_id,
                revision=int(chosen),
                operation_id=_chapter_ui.stable_operation_id("writer", "discard", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu revision đang là bản hiển thị/final candidate/final source thì tạo "
                "revision mới bằng Save/Regenerate rồi bỏ bản cũ."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()

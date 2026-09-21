"""Workspace Review: AI Review, sửa prose, Rewrite Section, Human Review, Finalize (T21).

Page này hiện thực `docs/design/workflow.md` mục 3.1, 3.2, 4.4 và D001/D002/D003:

- prose editor sửa tay rồi lưu qua `writer.save_draft` (revision mới, Human Review
  cũ mất hiệu lực — D002);
- **AI Review** chỉ là report hỗ trợ: UI hiển thị issue kèm `evidence.quote` và
  `source`, không coi report là gate (D001);
- **Rewrite Section**: chọn `selected_text` + instruction → `reviewer.rewrite_section`
  trả candidate replacement (không tạo revision) → user review rồi
  `reviewer.apply_rewrite` mới thay prose (D015);
- **Human Review** là gate cứng, gắn đúng prose revision (`reviewer.mark_reviewed`);
- **Finalize** gọi `reconcile.finalize_chapter`; UI nói rõ chapter chỉ thành
  `finalizing` và chương sau **vẫn khóa** cho tới khi reconciliation commit;
- draft **partial** không có nút Finalize khả dụng (backend vẫn là nơi chặn).
"""

from __future__ import annotations

from typing import Any

from novel_ai.core.models import ChapterStatus, ReviewReportPayload, RewriteSectionRequest
from novel_ai.services import ServiceError, reconcile, reviewer, writer
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "KEY_AI_ATTEMPT",
    "KEY_AI_FOCUS",
    "KEY_AI_RUN",
    "KEY_APPLY_REWRITE",
    "KEY_CHAPTER",
    "KEY_CONFIRM_REVIEW",
    "KEY_FINALIZE",
    "KEY_FINALIZE_ATTEMPT",
    "KEY_HUMAN_NOTES",
    "KEY_HUMAN_REVIEW",
    "KEY_PROSE_SAVE",
    "KEY_RELOAD_PROSE",
    "KEY_REPLACEMENT",
    "KEY_REWRITE_ATTEMPT",
    "KEY_REWRITE_CONSTRAINTS",
    "KEY_REWRITE_INSTRUCTION",
    "KEY_REWRITE_RUN",
    "KEY_SELECTED_TEXT",
    "REWRITE_CANDIDATE_KEY",
    "render",
]

KEY_CHAPTER = "novel_ai_review_chapter"
KEY_PROSE_SAVE = "novel_ai_review_prose_save"
KEY_RELOAD_PROSE = "novel_ai_review_reload_prose"
KEY_AI_FOCUS = "novel_ai_review_ai_focus"
KEY_AI_ATTEMPT = "novel_ai_review_ai_attempt"
KEY_AI_RUN = "novel_ai_review_ai_run"
KEY_SELECTED_TEXT = "novel_ai_review_selected_text"
KEY_REWRITE_INSTRUCTION = "novel_ai_review_rewrite_instruction"
KEY_REWRITE_CONSTRAINTS = "novel_ai_review_rewrite_constraints"
KEY_REWRITE_ATTEMPT = "novel_ai_review_rewrite_attempt"
KEY_REWRITE_RUN = "novel_ai_review_rewrite_run"
KEY_REPLACEMENT = "novel_ai_review_replacement"
KEY_APPLY_REWRITE = "novel_ai_review_apply_rewrite"
KEY_HUMAN_NOTES = "novel_ai_review_human_notes"
KEY_HUMAN_REVIEW = "novel_ai_review_human_review"
KEY_CONFIRM_REVIEW = "novel_ai_review_confirm_review"
KEY_FINALIZE = "novel_ai_review_finalize"
KEY_FINALIZE_ATTEMPT = "novel_ai_review_finalize_attempt"

#: Khóa session_state giữ candidate rewrite chưa apply (UI working state, không phải canon).
REWRITE_CANDIDATE_KEY = "novel_ai_review_rewrite_candidate"


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Review",
        "AI Review là report hỗ trợ; Human Review và Finalize mới là quyết định của bạn.",
    )
    show_action_result()
    _chapter_ui.show_note("review")

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Review.")
        return

    rows = _chapter_ui.chapter_rows(project)
    if not rows:
        st.info("Chưa có chapter nào. Hoàn tất Short Plan accepted trước.")
        return
    chapter_id = _chapter_ui.chapter_select(KEY_CHAPTER, rows)
    if chapter_id is None:  # pragma: no cover
        return

    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    if chapter is None:  # pragma: no cover
        return

    _render_state_panel(ctx, chapter_id)
    if chapter.status is ChapterStatus.final_reconciled:
        _render_retcon_notice(project, chapter_id)
        return

    _render_prose_panel(ctx, chapter_id)
    _render_ai_review_panel(ctx, chapter_id)
    _render_rewrite_panel(ctx, chapter_id)
    _render_human_review_panel(ctx, chapter_id)
    _render_finalize_panel(ctx, chapter_id)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _render_state_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    if chapter is None:  # pragma: no cover
        return
    st.caption(_chapter_ui.chapter_status_note(chapter.status.value))
    st.caption(_chapter_ui.human_review_line(chapter))
    draft = chapter.current_draft
    if draft is not None:
        st.markdown(
            f"Prose hiện tại: **r{draft.revision}** · "
            f"{'complete' if draft.is_complete else '**partial**'}"
        )

    check = reviewer.ready_to_finalize(project, chapter_id=chapter_id)
    st.subheader("Ready to finalize")
    if check.ready:
        st.success("Backend xác nhận chapter đủ điều kiện Finalize.")
    else:
        st.warning("Chưa đủ điều kiện Finalize. Lý do từ backend:")
        for reason in check.reasons:
            st.markdown(f"- {reason}")
    st.caption(
        "Cờ kiểm tra: draft `{draft}` · complete `{complete}` · human review hợp lệ "
        "`{human}` · skeleton fresh `{skeleton}` · chương trước ok `{previous}`".format(
            draft=check.has_draft,
            complete=check.is_complete,
            human=check.human_review_valid,
            skeleton=check.skeleton_fresh,
            previous=check.previous_chapter_ok,
        )
    )
    if not check.is_complete:
        st.warning(_chapter_ui.PARTIAL_NOTE)


def _render_retcon_notice(project: Any, chapter_id: str) -> None:
    """Chapter đã final: không sửa prose trực tiếp; retcon đi qua workspace Revision."""
    import streamlit as st

    marker = _chapter_ui.retcon_marker(project, chapter_id)
    st.subheader("Chapter đã `final_reconciled`")
    st.info(
        "Prose đã final không sửa trực tiếp: mọi thay đổi phải đi qua action retcon "
        "(workspace Revision). Final hiện tại vẫn là canon."
    )
    if marker is None:
        st.caption(
            "Bước tiếp theo: mở workspace **Revision** → `Start Retcon` cho chương này nếu "
            "bạn thật sự muốn thay bản final."
        )
        return
    st.warning(
        "Chương này **đang retcon**: draft retcon r"
        f"{marker.get('retcon_draft_revision')} đã được tạo từ final r"
        f"{marker.get('replaces_final_revision')}. Final cũ **vẫn là canon** tới khi "
        "retcon commit (D004)."
    )
    if st.button("Finalize bản retcon (xác nhận Human Review trong action)", key=KEY_FINALIZE):
        intent = (chapter_id, "retcon", marker.get("retcon_draft_revision"))
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_FINALIZE,
                intent=intent,
                project=project,
                call=lambda: reconcile.finalize_chapter(
                    project,
                    chapter_id=chapter_id,
                    confirm_review=True,
                    operation_id=_chapter_ui.stable_operation_id(
                        "review", "finalize_retcon", chapter_id, marker.get("retcon_draft_revision")
                    ),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Kiểm tra lại guard trong workspace Reconcile/Revision; final cũ vẫn là "
                    "canon nên không mất dữ liệu."
                ),
            )
            return
        if blocked:
            return
        set_action_result(result)
        _chapter_ui.set_note(
            "review",
            "Bản retcon đã được đóng băng thành final candidate (chapter `finalizing`). "
            "Bước tiếp theo: workspace **Reconcile** để generate/accept reconciliation cho retcon.",
        )
        st.rerun()


# ---------------------------------------------------------------------------
# Prose
# ---------------------------------------------------------------------------


def _render_prose_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    if chapter is None:  # pragma: no cover
        return
    st.subheader("Prose (edit + Save Draft)")
    draft = chapter.current_draft
    if draft is None:
        st.info("Chưa có prose revision. Chạy Generate ở workspace Writer trước.")
        return
    text = _chapter_ui.prose_text(project, chapter_id, draft.revision)
    key = f"novel_ai_review_prose_{chapter_id}"
    version = (chapter_id, draft.revision, _chapter_ui.payload_fingerprint(text))
    if st.button("Nạp lại editor từ prose hiện tại", key=KEY_RELOAD_PROSE):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    current = _common.sync_text_editor(key, text, version=version)
    edited = st.text_area(
        "Prose (markdown) — Save tạo prose revision mới và làm Human Review cũ mất hiệu lực",
        value=current,
        height=320,
        key=key,
    )
    if not st.button("Lưu prose đã sửa (Save Draft)", key=KEY_PROSE_SAVE):
        return
    intent = (chapter_id, _chapter_ui.payload_fingerprint(edited))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_PROSE_SAVE,
            intent=intent,
            project=project,
            call=lambda: writer.save_draft(
                project,
                chapter_id=chapter_id,
                text=edited,
                operation_id=_chapter_ui.stable_operation_id("review", "save_draft", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step="Sửa nội dung rồi lưu lại; accepted/final không bị thay.",
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(key, f"{key}__source")
    st.rerun()


# ---------------------------------------------------------------------------
# AI review
# ---------------------------------------------------------------------------


def _render_ai_review_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("AI Review (report hỗ trợ, không phải gate)")
    st.caption(
        "Report gắn với `prose_revision`; report **không** sửa prose, không đổi chapter "
        "status, không finalize (D001)."
    )
    focus = st.text_input("Trọng tâm review (tuỳ chọn)", key=KEY_AI_FOCUS)
    attempt = _chapter_ui.attempt_input(KEY_AI_ATTEMPT)

    if ctx.llm_client is None or ctx.registry is None:
        st.error(
            "Chưa dùng được AI Review: "
            + ("thiếu prompt registry. " if ctx.registry is None else "")
            + ("chưa dựng được LLM client. " if ctx.llm_client is None else "")
            + (ctx.llm_error or "")
        )
    elif st.button("Chạy AI Review", key=KEY_AI_RUN):
        intent = (chapter_id, str(focus or "").strip(), int(attempt))
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_AI_RUN,
                intent=intent,
                project=project,
                call=lambda: reviewer.run_ai_review(
                    project,
                    client=ctx.llm_client,
                    chapter_id=chapter_id,
                    review_focus=str(focus or "").strip(),
                    operation_id=_chapter_ui.stable_operation_id("review", "ai_review", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Lỗi report không khóa Human Review: bạn vẫn review tay và Finalize "
                    "được. Thử lại nếu muốn report."
                ),
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()

    _render_stored_report(project, chapter_id)


def _render_stored_report(project: Any, chapter_id: str) -> None:
    """Hiển thị report đã lưu (kèm evidence.quote/source) — sống qua reload."""
    import streamlit as st

    artifact_id = f"review_report_{chapter_id}"
    envelope = _common.envelope_or_none(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        st.caption("Chưa có AI Review report cho chapter này.")
        return
    payload = envelope.accepted_revision.payload
    if not isinstance(payload, ReviewReportPayload):
        try:
            payload = ReviewReportPayload.model_validate(payload.model_dump(mode="json"))
        except Exception:  # pragma: no cover - report hỏng không làm crash UI
            st.warning("Report đã lưu không parse được theo contract hiện tại.")
            return
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    current = chapter.current_draft_revision if chapter is not None else None
    if payload.prose_revision != current:
        st.warning(
            f"Report gắn prose r{payload.prose_revision} nhưng draft hiện tại là r{current}: "
            "report chỉ còn là tài liệu lịch sử."
        )
    st.markdown(
        f"**Report r{envelope.accepted_revision.revision}** (prose r{payload.prose_revision}) — "
        f"{payload.summary}"
    )
    if not payload.issues:
        st.caption("Report không nêu issue nào.")
    for issue in payload.issues:
        st.markdown(
            f"- **[{issue.severity.value}]** `{issue.category}` · {issue.message}"
        )
        source = issue.source
        st.caption(
            "Nguồn: "
            f"`{source.authority_kind}`"
            + (f" · artifact `{source.artifact_id}`" if source.artifact_id else "")
            + (f" · r{source.revision}" if source.revision else "")
            + (f" · field `{source.field_path}`" if source.field_path else "")
        )
        if issue.evidence.quote:
            st.code(issue.evidence.quote, language="text")
        elif issue.evidence.section_id:
            st.caption(f"Evidence: section `{issue.evidence.section_id}`")
        if issue.suggested_action:
            st.caption(f"Gợi ý: {issue.suggested_action}")


# ---------------------------------------------------------------------------
# Rewrite section
# ---------------------------------------------------------------------------


def _render_rewrite_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    if chapter is None or chapter.current_draft is None:
        return
    draft = chapter.current_draft
    st.subheader("Rewrite một đoạn cụ thể")
    st.caption(
        "Rewrite **không** tạo revision: backend trả candidate replacement; bạn phải "
        "review rồi bấm Apply mới thay prose (D015)."
    )
    selected = st.text_area(
        "Đoạn prose cần rewrite (`selected_text`; để trống = gửi toàn draft làm target)",
        key=KEY_SELECTED_TEXT,
        height=120,
    )
    instruction = st.text_input("Instruction cho rewrite", key=KEY_REWRITE_INSTRUCTION)
    constraints = st.text_area(
        "Ràng buộc thêm (mỗi dòng một mục, tuỳ chọn)", key=KEY_REWRITE_CONSTRAINTS, height=80
    )
    attempt = _chapter_ui.attempt_input(KEY_REWRITE_ATTEMPT)

    if ctx.llm_client is None or ctx.registry is None:
        st.error("Chưa dùng được Rewrite: thiếu prompt registry hoặc LLM client.")
    elif st.button("Tạo replacement candidate", key=KEY_REWRITE_RUN):
        request = RewriteSectionRequest(
            chapter_id=chapter_id,
            prose_revision=draft.revision,
            selected_text=str(selected).strip() or None,
            instruction=str(instruction or "").strip(),
            constraints=[
                line.strip()
                for line in str(constraints or "").splitlines()
                if line.strip()
            ],
        )
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_REWRITE_RUN,
                intent=(
                    chapter_id,
                    draft.revision,
                    request.selected_text or "",
                    request.instruction,
                    tuple(request.constraints),
                    int(attempt),
                ),
                project=project,
                call=lambda: reviewer.rewrite_section(
                    project,
                    client=ctx.llm_client,
                    request=request,
                    operation_id=_chapter_ui.stable_operation_id(
                        "review",
                        "rewrite",
                        chapter_id,
                        draft.revision,
                        request.selected_text or "",
                        request.instruction,
                        request.constraints,
                        attempt,
                    ),
                ),
                remember=True,
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Nếu `selected_text` không có trong prose hiện tại, copy lại đúng đoạn "
                    "trong revision đang hiển thị rồi chạy lại."
                ),
            )
        else:
            if blocked:
                return
            st.session_state[REWRITE_CANDIDATE_KEY] = {
                "chapter_id": chapter_id,
                "prose_revision": result.data.get("prose_revision"),
                "target_text": result.data.get("target_text") or "",
                "replacement_markdown": result.data.get("replacement_markdown") or "",
                "changed_intent": bool(result.data.get("changed_intent")),
                "notes": list(result.data.get("notes") or []),
            }
            set_action_result(result)
            st.rerun()

    _render_rewrite_candidate(ctx, chapter_id)


def _render_rewrite_candidate(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    candidate = st.session_state.get(REWRITE_CANDIDATE_KEY)
    if not isinstance(candidate, dict) or candidate.get("chapter_id") != chapter_id:
        return
    st.markdown(
        f"**Replacement candidate** cho prose r{candidate.get('prose_revision')} "
        "(chưa apply — không có revision nào được tạo)."
    )
    if candidate.get("changed_intent"):
        st.warning(
            "`changed_intent=True`: rewrite có thể vượt quyền Writer; cân nhắc sửa "
            "upstream (Skeleton/plan) thay vì chỉ apply prose."
        )
    for note in candidate.get("notes") or []:
        st.caption(f"Ghi chú từ LLM: {note}")
    with st.expander("Target text đã dùng (nguyên văn)"):
        st.code(str(candidate.get("target_text") or ""), language="text")
    key = KEY_REPLACEMENT
    version = (
        chapter_id,
        candidate.get("prose_revision"),
        _chapter_ui.payload_fingerprint(candidate.get("replacement_markdown")),
    )
    text = _common.sync_text_editor(
        key, str(candidate.get("replacement_markdown") or ""), version=version
    )
    edited = st.text_area("Replacement (có thể sửa trước khi apply)", value=text, height=200, key=key)
    apply_col, discard_col = st.columns(2)
    with apply_col:
        apply_clicked = st.button("Apply rewrite vào prose", key=KEY_APPLY_REWRITE)
    with discard_col:
        discard_clicked = st.button("Bỏ candidate rewrite", key="novel_ai_review_discard_rewrite")
    if discard_clicked:
        _common.clear_session_keys(REWRITE_CANDIDATE_KEY, key, f"{key}__source")
        st.rerun()
    if not apply_clicked:
        return
    intent = (
        chapter_id,
        candidate.get("prose_revision"),
        _chapter_ui.payload_fingerprint(str(candidate.get("target_text") or "")),
        _chapter_ui.payload_fingerprint(edited),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_APPLY_REWRITE,
            intent=intent,
            project=project,
            call=lambda: reviewer.apply_rewrite(
                project,
                chapter_id=chapter_id,
                replacement_markdown=edited,
                target_text=str(candidate.get("target_text") or ""),
                expected_revision=int(candidate.get("prose_revision") or 0),
                operation_id=_chapter_ui.stable_operation_id("review", "apply_rewrite", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu replacement đã stale (prose đổi), tạo lại rewrite trên revision hiện "
                "tại; draft hiện tại không bị thay."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(REWRITE_CANDIDATE_KEY, key, f"{key}__source")
    st.rerun()


# ---------------------------------------------------------------------------
# Human review + finalize
# ---------------------------------------------------------------------------


def _render_human_review_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    st.subheader("Human Review (gate cứng)")
    st.caption(_chapter_ui.human_review_line(chapter) if chapter else "Chưa đọc được chapter.")
    notes = st.text_area("Ghi chú review (tuỳ chọn)", key=KEY_HUMAN_NOTES, height=80)
    if not st.button("Xác nhận Human Review cho prose hiện tại", key=KEY_HUMAN_REVIEW):
        return
    intent = (
        chapter_id,
        chapter.current_draft_revision if chapter else None,
        str(notes or "").strip(),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_HUMAN_REVIEW,
            intent=intent,
            project=project,
            call=lambda: reviewer.mark_reviewed(
                project,
                chapter_id=chapter_id,
                notes=str(notes or "").strip(),
                operation_id=_chapter_ui.stable_operation_id("review", "human_review", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Human Review cần draft hiện tại đã complete. Nếu prose vừa đổi, review lại "
                "revision mới; backend không tự chuyển revision."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _render_finalize_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Finalize Chapter")
    st.caption(_chapter_ui.FINALIZE_LOCK_NOTE)
    check = reviewer.ready_to_finalize(project, chapter_id=chapter_id)
    draft = _chapter_ui.chapter_by_id(project, chapter_id)
    prose_revision = draft.current_draft_revision if draft is not None else None

    if not check.is_complete:
        st.warning(_chapter_ui.PARTIAL_NOTE)
    confirm = st.checkbox(
        "Xác nhận Human Review trong chính action Finalize (dùng khi bạn vừa đọc prose)",
        value=False,
        key=KEY_CONFIRM_REVIEW,
    )
    attempt = _chapter_ui.attempt_input(
        KEY_FINALIZE_ATTEMPT,
        help_text=(
            "Bấm lặp cùng số sẽ không finalize lần hai. Sau khi `Cancel finalizing` ở "
            "workspace Reconcile, tăng số này để Finalize lại chính revision đó."
        ),
    )
    clicked = st.button(
        "Finalize Chapter",
        key=KEY_FINALIZE,
        disabled=not check.is_complete,
        help="" if check.is_complete else _chapter_ui.PARTIAL_NOTE,
    )
    if not clicked:
        return
    intent = (chapter_id, prose_revision, bool(confirm), int(attempt))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_FINALIZE,
            intent=intent,
            project=project,
            call=lambda: reconcile.finalize_chapter(
                project,
                chapter_id=chapter_id,
                confirm_review=bool(confirm),
                operation_id=_chapter_ui.stable_operation_id("review", "finalize", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Đọc lý do ở panel `Ready to finalize`. Human Review phải gắn đúng prose "
                "revision hiện tại (hoặc tick xác nhận trong action này)."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _chapter_ui.set_note(
        "review",
        "Chapter vừa chuyển `finalizing`; chương sau **vẫn khóa**. Bước tiếp theo: mở "
        "workspace **Reconcile** để generate/accept reconciliation.",
    )
    st.rerun()


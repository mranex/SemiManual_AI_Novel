"""Workspace Skeleton: generate/regenerate, sửa candidate, accept/reject (T21).

Page này hiện thực `docs/design/workflow.md` mục 4.3 và D014/D006 trong phạm vi
UI:

- hiển thị **guard và nguồn dependency trước**: Short Plan phải `accepted` (hợp
  đồng contract "Skeleton chỉ accept khi Short Plan accepted"), context basis
  `actual`/`provisional` của candidate, và pin thật đã dùng;
- candidate **provisional** bị nêu rõ: chỉ là chuẩn bị trước, **không dùng được
  cho Writer** và **không accept được** cho tới khi regenerate/review trên
  context `actual` (đây là guard backend của `skeleton.accept`, UI chỉ nói trước);
- mọi mutation (`skeleton.generate`, `edit_candidate`, `accept`, `reject`) nằm sau
  `st.button` trong nhánh `if clicked:`; rerun thuần không gọi LLM và không ghi
  file;
- `operation_id` là hàm tất định của input (`_chapter_ui.stable_operation_id`) nên
  bấm lặp/double submit được service replay, không tạo candidate thứ hai;
- Skeleton **luôn** là candidate cho tới khi user accept; Auto Accept structured
  không áp cho việc tự mở Writer.
"""

from __future__ import annotations

from typing import Any

from novel_ai.core import lifecycle
from novel_ai.core.models import ArtifactStatus
from novel_ai.services import ServiceError, skeleton
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "ACTIONS",
    "KEY_ACCEPT",
    "KEY_CHAPTER",
    "KEY_EDIT",
    "KEY_GENERATE",
    "KEY_REJECT",
    "KEY_RELOAD",
    "artifact_id_for_chapter",
    "render",
]

#: Action context của `skeleton.generate` (khớp `core.context.build_skeleton_context`).
ACTIONS: tuple[str, ...] = ("generate", "regenerate")

KEY_CHAPTER = "novel_ai_skeleton_chapter"
KEY_GENERATE = "novel_ai_skeleton_generate"
KEY_EDIT = "novel_ai_skeleton_edit"
KEY_ACCEPT = "novel_ai_skeleton_accept"
KEY_REJECT = "novel_ai_skeleton_reject"
KEY_RELOAD = "novel_ai_skeleton_reload"
KEY_ACTION = "novel_ai_skeleton_action"
KEY_INSTRUCTION = "novel_ai_skeleton_instruction"
KEY_ATTEMPT = "novel_ai_skeleton_attempt"


def artifact_id_for_chapter(chapter_id: str) -> str:
    """Artifact ID Skeleton của chapter (`skeleton_<chapter_id>`)."""
    return lifecycle.artifact_id_for("skeleton", chapter_id=chapter_id)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Skeleton",
        "Skeleton là chỉ dẫn chi tiết của một chương; candidate chỉ thành canon sau khi bạn accept.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Skeleton.")
        return

    rows = _chapter_ui.chapter_rows(project)
    if not rows:
        st.info(
            "Chưa có chapter nào. Bước tiếp theo: hoàn tất Short Plan accepted để "
            "backend tạo chapter metadata trước khi viết Skeleton."
        )
        return

    chapter_id = _chapter_ui.chapter_select(KEY_CHAPTER, rows)
    if chapter_id is None:  # pragma: no cover - rows không rỗng nên luôn có lựa chọn
        return

    _render_dependency_panel(ctx, chapter_id)
    _render_state_panel(project, chapter_id)
    _render_generate_panel(ctx, chapter_id)
    _render_candidate_panel(ctx, chapter_id)


def _render_dependency_panel(ctx: AppContext, chapter_id: str) -> None:
    """Guard + nguồn dependency: Short Plan accepted, context basis và pin."""
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Guard và nguồn dependency")

    short_plan = _common.envelope_or_none(project, "short_plan")
    if short_plan is None or short_plan.accepted_revision is None:
        st.error(
            "Short Plan chưa có accepted revision: Skeleton chỉ generate/accept được "
            "khi Short Plan accepted."
        )
    elif short_plan.status is not ArtifactStatus.accepted:
        st.warning(
            f"Short Plan đang `{short_plan.status.value}`: hãy review/reaccept hoặc "
            "regenerate Short Plan trước khi dùng Skeleton của chương này."
        )
    else:
        st.caption(
            "Short Plan: "
            + _common.artifact_caption(short_plan, label="short_plan")
            + " → nguồn chapter/ID hợp lệ cho Skeleton."
        )

    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    if chapter is None:  # pragma: no cover - chapter lấy từ rows
        return
    st.caption(f"Trạng thái chapter: {_chapter_ui.chapter_status_note(chapter.status.value)}")

    artifact_id = artifact_id_for_chapter(chapter_id)
    envelope = _common.envelope_or_none(project, artifact_id)
    st.caption(_common.artifact_caption(envelope, label=artifact_id))

    basis = _chapter_ui.provisional_basis(chapter)
    if basis is None:
        st.caption(
            "Context basis: chưa có dấu `preparation_context` (candidate không phải "
            "bản chuẩn bị trước hoặc chưa generate lần nào)."
        )
        return
    st.markdown(
        f"**Context basis hiện tại:** mode `{basis['mode']}` · "
        f"actual tới chương {basis['actual_through_chapter']} · "
        f"{len(basis['planned_bridge'])} planned bridge"
    )
    for item in basis["planned_bridge"]:
        st.markdown(
            f"- bridge chương {item['chapter_number']} · `{item['chapter_id']}` — {item['summary']}"
        )
    pins = basis.get("dependency_pins") or []
    if pins:
        st.caption("Pin của basis: " + " · ".join(_pin_text(pin) for pin in pins))
    if basis["mode"] == "provisional":
        st.warning(_chapter_ui.PROVISIONAL_NOTE)
        st.info(
            "Bước tiếp theo: đợi chương trước `final_reconciled` rồi `regenerate` (hoặc "
            "sửa tay) để candidate dựa trên context `actual`, sau đó mới accept."
        )
    else:
        st.caption(
            "Mode `actual`: candidate dựa trên state thật trước chương này; accept "
            "được nếu pin còn khớp accepted hiện tại."
        )


def _pin_text(pin: Any) -> str:
    """Pin dict (từ `model_dump`) -> chuỗi ngắn cho caption."""
    if isinstance(pin, dict):
        return f"`{pin.get('artifact_id')}` r{pin.get('revision')}"
    return str(pin)


def _render_state_panel(project: Any, chapter_id: str) -> None:
    import streamlit as st

    artifact_id = artifact_id_for_chapter(chapter_id)
    envelope = _common.envelope_or_none(project, artifact_id)
    st.subheader("Candidate vs accepted")
    if envelope is None:
        st.caption("Chưa có Skeleton cho chương này; hãy generate trước.")
        return
    if envelope.accepted_revision is not None:
        st.markdown(
            f"**Accepted r{envelope.accepted_revision.revision}** "
            f"(source `{envelope.accepted_revision.payload_source.source_type.value}`)"
        )
        pin_lines = _chapter_ui.pin_lines(envelope.accepted_revision.dependency_pins)
        st.caption("Pin: " + (" · ".join(pin_lines) if pin_lines else "(không có pin)"))
    else:
        st.caption("Chưa có accepted revision: Writer vẫn bị khóa cho chương này.")
    if envelope.candidate_revision is not None:
        st.markdown(f"**Candidate r{envelope.candidate_revision.revision}** (chưa accept)")
    for note in _common.artifact_stale_notes(envelope):
        st.warning(note)
    if envelope.status is ArtifactStatus.stale:
        st.info(
            "Skeleton đang `stale`: Writer chỉ chạy với Skeleton accepted/fresh. Hãy "
            "review/reaccept trong workspace Revision, hoặc regenerate candidate mới."
        )
    with st.expander("Xem payload accepted (canon hiện tại)"):
        if envelope.accepted_revision is None:
            st.caption("Chưa có accepted revision.")
        else:
            st.json(envelope.accepted_revision.payload.model_dump(mode="json"))


def _render_generate_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Generate/Regenerate Skeleton")
    st.caption(
        "Generate ghi raw output trước khi parse, map ID tạm `tmp_section_<n>` sang stable "
        "ID, validate cross-field rồi lưu **candidate**. Accepted cũ không đổi."
    )
    action = st.selectbox("Action", ACTIONS, key=KEY_ACTION)
    instruction = st.text_area(
        "Yêu cầu thêm cho lần chạy (tuỳ chọn)",
        key=KEY_INSTRUCTION,
        height=80,
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

    if not st.button("Generate Skeleton candidate", key=KEY_GENERATE):
        return
    intent = (chapter_id, str(action), str(instruction or "").strip(), int(attempt))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_GENERATE,
            intent=intent,
            project=project,
            call=lambda: skeleton.generate(
                project,
                client=ctx.llm_client,
                chapter_id=chapter_id,
                action=str(action),
                user_instruction=str(instruction or "").strip(),
                operation_id=_chapter_ui.stable_operation_id(
                    "skeleton", "generate", *intent
                ),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Đọc lỗi theo field ở trên. Nếu là guard (Short Plan/pin/provisional), "
                "xử lý dependency trước rồi chạy lại; accepted cũ vẫn nguyên."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _render_candidate_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    artifact_id = artifact_id_for_chapter(chapter_id)
    envelope = _common.envelope_or_none(project, artifact_id)
    st.subheader("Sửa và accept/reject candidate")
    if envelope is None or envelope.candidate_revision is None:
        st.caption("Chưa có candidate nào đang chờ accept.")
        return

    candidate = envelope.candidate_revision
    accepted = envelope.accepted_revision
    st.caption(_common.candidate_update_hint_for_envelope(envelope))
    if candidate.validation.errors:
        st.warning("Candidate còn lỗi validation đã ghi nhận khi tạo:")
        for issue in candidate.validation.errors:
            st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")

    key = _common.editor_key("skeleton", workspace="skeleton")
    version = (
        chapter_id,
        f"c{candidate.revision}:{_chapter_ui.payload_fingerprint(candidate.payload)}",
        f"a{accepted.revision if accepted else 0}:"
        f"{_chapter_ui.payload_fingerprint(accepted.payload if accepted else None)}",
    )
    if st.button("Nạp lại editor từ candidate", key=KEY_RELOAD):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    text = _common.sync_text_editor(
        key, _common.payload_json_text(candidate.payload), version=version
    )
    edited = st.text_area(
        "Candidate Skeleton (JSON; phải dùng stable ID, không dùng `tmp_section_*`)",
        value=text,
        height=300,
        key=key,
    )

    st.caption("Pin của candidate: " + " · ".join(
        _chapter_ui.pin_lines(candidate.dependency_pins) or ["(không có pin)"]
    ))
    mismatches = _chapter_ui.pin_mismatches(project, candidate.dependency_pins)
    for message in mismatches:
        st.warning(f"Pin lệch accepted hiện tại: {message}")

    save_col, accept_col, reject_col = st.columns(3)
    with save_col:
        save_clicked = st.button("Lưu candidate đã sửa", key=KEY_EDIT)
    with accept_col:
        accept_clicked = st.button("Accept Skeleton", key=KEY_ACCEPT)
    with reject_col:
        reject_clicked = st.button("Reject candidate", key=KEY_REJECT)

    if save_clicked:
        payload, parse_error = _common.parse_json_payload(edited)
        if parse_error:
            st.error(f"Chưa lưu được candidate: {parse_error}")
            st.info("Bước tiếp theo: sửa JSON trong editor (nội dung bạn nhập vẫn còn) rồi lưu lại.")
        else:
            intent = (chapter_id, _chapter_ui.payload_fingerprint(payload))
            try:
                blocked, result = _chapter_ui.run_action(
                    KEY_EDIT,
                    intent=intent,
                    project=project,
                    call=lambda: skeleton.edit_candidate(
                        project,
                        chapter_id=chapter_id,
                        payload=payload,
                        operation_id=_chapter_ui.stable_operation_id("skeleton", "edit", *intent),
                    ),
                )
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Sửa đúng field được nêu ở trên (stable ID, đúng chapter_id/"
                        "chapter_number) rồi lưu lại; accepted giữ nguyên."
                    ),
                )
            else:
                if blocked:
                    return
                set_action_result(result)
                _common.clear_session_keys(key, f"{key}__source")
                st.rerun()
    if accept_clicked:
        intent = (chapter_id, candidate.revision)
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_ACCEPT,
                intent=intent,
                project=project,
                call=lambda: skeleton.accept(
                    project,
                    chapter_id=chapter_id,
                    operation_id=_chapter_ui.stable_operation_id("skeleton", "accept", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Nếu là provisional/stale/pin lệch: regenerate hoặc sửa candidate trên "
                    "context actual rồi accept lại. Accepted cũ vẫn nguyên."
                ),
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()
    if reject_clicked:
        intent = (chapter_id, candidate.revision)
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_REJECT,
                intent=intent,
                project=project,
                call=lambda: skeleton.reject(
                    project,
                    chapter_id=chapter_id,
                    operation_id=_chapter_ui.stable_operation_id("skeleton", "reject", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Candidate có thể đã bị reject trước đó; kiểm tra lại state rồi "
                    "generate candidate mới nếu cần."
                ),
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()

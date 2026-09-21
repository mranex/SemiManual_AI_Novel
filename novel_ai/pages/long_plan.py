"""Workspace Long Plan: Volume/Arc candidate, accept và reject (T20).

Page này hiện thực `docs/design/workflow.md` mục 4.2 và
`novel_ai_spec_v0.2.md` mục 34: Long Plan quản lý Volume → Arc, không viết prose
và không chứng minh sự kiện đã xảy ra.

Luật đã giữ trong page:

- mọi mutation (`long_planner.generate`, `accept`, `reject`) nằm sau
  `st.button`/`st.form_submit_button` trong nhánh `if submitted:`; rerun thuần
  không gọi LLM và không ghi file;
- `planning_scope` là input rõ ràng của user, kiểm tra `start <= end` trước khi
  gọi service (service vẫn kiểm tra lại);
- candidate và accepted được hiển thị **cạnh nhau**; pin và lý do stale hiển thị
  để user biết vì sao artifact không dùng được cho action phụ thuộc;
- ID (`vol_`/`arc_`) hiển thị kèm tiêu đề và phạm vi chương cho dễ đọc.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novel_ai.core.models import ArtifactStatus, LongPlanPayload
from novel_ai.services import GuardError, ServiceError, long_planner
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = ["ACTIONS", "build_planning_scope", "render"]

ACTIONS: tuple[str, ...] = ("generate", "regenerate", "edit")


def build_planning_scope(start: int, end: int) -> dict[str, int]:
    """Dựng `planning_scope` từ input UI và kiểm tra `1 <= start <= end`."""
    start_value = int(start)
    end_value = int(end)
    if start_value < 1 or end_value < start_value:
        raise GuardError(
            f"`planning_scope` không hợp lệ: start={start_value}, end={end_value}; "
            "cần 1 <= start <= end.",
            code="invalid_planning_scope",
        )
    return {"start": start_value, "end": end_value}


def _plan_lines(payload: LongPlanPayload, index: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for volume in payload.volumes:
        lines.append(f"### `{volume.volume_id}` — {volume.title}")
        lines.append(f"- Theme: {volume.theme}")
        lines.append(f"- Goal: {volume.goal}")
        for arc in volume.arcs:
            lines.append(
                f"- **`{arc.arc_id}`** {arc.title} · ch.{arc.chapter_range.start}–"
                f"{arc.chapter_range.end}"
            )
            lines.append(f"  - Goal: {arc.goal}")
            lines.append(f"  - Core conflict: {arc.core_conflict}")
            lines.append(f"  - Start → End: {arc.start_state} → {arc.end_state}")
            if arc.major_reveals:
                lines.append(f"  - Major reveals: {'; '.join(arc.major_reveals)}")
            lines.append(f"  - Nhân vật: {_common.format_id_list(arc.character_ids, index)}")
            lines.append(
                f"  - World rule: {_common.format_id_list(arc.world_rule_ids, index)}"
            )
            lines.append(
                f"  - Foreshadow: {_common.format_id_list(arc.foreshadow_ids, index)}"
            )
            for direction in arc.relationship_directions:
                lines.append(
                    "  - Hướng quan hệ (tương lai): "
                    f"{_common.format_id_list(direction.character_ids, index)} → "
                    f"{direction.target_state}"
                )
    if payload.global_threads:
        lines.append("### Global threads")
        for thread in payload.global_threads:
            lines.append(f"- {_common.compact_json(thread)}")
    return lines


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Long Plan",
        "Volume → Arc. Long Plan là ý định tương lai, không phải sự kiện đã xảy ra.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Long Plan.")
        return

    envelope = _common.envelope_or_none(project, "long_plan")
    index = _common.build_id_index(project)
    st.caption(_common.artifact_caption(envelope, label="long_plan"))
    for note in _common.artifact_stale_notes(envelope):
        st.warning(note)
    if envelope is not None and envelope.status is ArtifactStatus.stale:
        st.info(
            "Long Plan đang `stale`: regenerate/review rồi accept lại. Stale accepted "
            "không dùng được cho Short Plan."
        )

    _render_generate_form(ctx)
    _render_candidate(ctx, index)


def _render_generate_form(ctx: AppContext) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Generate / Regenerate Long Plan")
    st.caption(
        "Guard backend: Base Idea accepted + Premise accepted và không stale. Volume/arc "
        "ID do backend reserve; ID tạm `tmp_volume_*`/`tmp_arc_*` được map sang stable ID."
    )
    default_scope = long_planner.resolve_planning_scope(project)
    with st.form("novel_ai_long_plan_generate"):
        action = st.selectbox("Action", ACTIONS, key="novel_ai_long_plan_action")
        col_start, col_end = st.columns(2)
        with col_start:
            scope_start = st.number_input(
                "planning_scope.start",
                min_value=1,
                value=int(default_scope["start"]),
                step=1,
                key="novel_ai_long_plan_scope_start",
            )
        with col_end:
            scope_end = st.number_input(
                "planning_scope.end",
                min_value=1,
                value=int(default_scope["end"]),
                step=1,
                key="novel_ai_long_plan_scope_end",
            )
        instruction = st.text_area(
            "Yêu cầu thêm (tuỳ chọn)", key="novel_ai_long_plan_instruction", height=80
        )
        attempt = st.number_input(
            _chapter_ui.ATTEMPT_LABEL,
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            key="novel_ai_long_plan_attempt",
        )
        submitted = st.form_submit_button("Chạy generate/regenerate")
    st.caption(
        f"`planning_scope` đang suy từ state: {default_scope['start']}–{default_scope['end']} "
        "(tối thiểu 1–3, mở rộng theo plan/chapter hiện có)."
    )
    if not submitted:
        return
    try:
        scope = build_planning_scope(int(scope_start), int(scope_end))
    except GuardError as error:
        _common.render_service_error(
            error, next_step="Sửa `planning_scope` thành 1 <= start <= end rồi chạy lại."
        )
        return
    if ctx.llm_client is None:
        st.error(
            "Chưa dựng được LLM client nên không gọi được `long_planner.generate`."
            + (f" {ctx.llm_error}" if ctx.llm_error else "")
        )
        return
    operation_id = _chapter_ui.stable_operation_id(
        "long_plan",
        str(action),
        f"{scope['start']}-{scope['end']}",
        str(instruction or "").strip(),
        int(attempt),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            "novel_ai_long_plan_generate",
            intent=("generate", operation_id),
            project=project,
            call=lambda: long_planner.generate(
                project,
                client=ctx.llm_client,
                planning_scope=scope,
                action=str(action),
                user_instruction=str(instruction or "").strip(),
                operation_id=operation_id,
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu là guard: accept Base Idea/Premise trước. Nếu là validation: "
                "xem field sai rồi regenerate. Accepted Long Plan cũ không bị thay đổi."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _render_candidate(ctx: AppContext, index: Mapping[str, str]) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, "long_plan")
    st.subheader("Candidate vs accepted")
    if envelope is None or envelope.candidate_revision is None:
        st.caption("Chưa có candidate nào đang chờ accept.")
    else:
        candidate = envelope.candidate_revision
        accepted = envelope.accepted_revision
        st.caption(_common.candidate_update_hint_for_envelope(envelope))
        st.caption(
            f"Candidate r{candidate.revision} · validation `{candidate.validation.state.value}`"
            + (
                f" · accepted r{accepted.revision}"
                if accepted is not None
                else " · chưa có accepted"
            )
        )
        for issue in candidate.validation.errors:
            st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")
        for pin in candidate.dependency_pins:
            st.caption(
                f"pin `{pin.artifact_id}` r{pin.revision} (scope `{pin.scope}`)"
            )
        tabs = st.tabs(["Candidate (dễ đọc)", "Accepted (dễ đọc)", "JSON thô"])
        with tabs[0]:
            st.markdown("\n".join(_plan_lines(candidate.payload, index)))
        with tabs[1]:
            if accepted is None:
                st.caption("Chưa có accepted Long Plan.")
            else:
                st.markdown("\n".join(_plan_lines(accepted.payload, index)))
        with tabs[2]:
            st.json(candidate.payload.model_dump(mode="json"))
            if accepted is not None:
                st.caption("Accepted (JSON):")
                st.json(accepted.payload.model_dump(mode="json"))

        accept_col, reject_col = st.columns(2)
        with accept_col:
            accept_clicked = st.button("Accept Long Plan", key="novel_ai_long_plan_accept")
        with reject_col:
            reject_clicked = st.button("Reject candidate", key="novel_ai_long_plan_reject")
        if accept_clicked:
            try:
                result = long_planner.accept(project)
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Regenerate nếu pin lệch hoặc range sai; accept chỉ chạy khi "
                        "candidate hợp lệ và dependency còn fresh."
                    ),
                )
            else:
                set_action_result(result)
                st.rerun()
        if reject_clicked:
            try:
                result = long_planner.reject(project)
            except ServiceError as error:
                _common.render_service_error(
                    error, next_step="Kiểm tra lại state; candidate có thể đã bị reject."
                )
            else:
                set_action_result(result)
                st.rerun()

    accepted_envelope = envelope
    if accepted_envelope is None or accepted_envelope.accepted_revision is None:
        st.info(
            "Chưa có Long Plan accepted. Bước tiếp theo: generate rồi accept để mở Short Plan."
        )
    else:
        st.caption(
            _common.selected_revision_text(accepted_envelope, which="accepted")
        )

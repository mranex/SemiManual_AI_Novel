"""Workspace Long Plan: Volume/Arc candidate, accept và reject (T20, horizon T30).

Page này hiện thực `docs/design/workflow.md` mục 4.2 và
`novel_ai_spec_v0.2.md` mục 34: Long Plan quản lý Volume → Arc, không viết prose
và không chứng minh sự kiện đã xảy ra.

Luật đã giữ trong page:

- mọi mutation (`long_planner.generate`, `accept`, `reject`,
  `confirm_planning_scope`) nằm sau `st.button`/`st.form_submit_button` trong nhánh
  `if submitted:`; rerun thuần không gọi LLM và không ghi file;
- **`planning_scope` là toàn horizon cần kiến trúc** (D017), user nhập rõ; page
  **không** prefill từ `current_chapter`/Short Plan/chapter metadata và không có
  default ngầm `1..3`. Khi đã có horizon lưu trên revision thì prefill đúng giá trị
  đó để regenerate/edit dùng lại;
- preview coverage (volume/arc/range, số chương phủ, khoảng thiếu) và warning
  non-blocking khi plan chỉ có một arc — đây là tín hiệu UX, không phải validator;
- accepted revision legacy thiếu `planning_scope` hiện banner riêng và chỉ migrate
  bằng action tường minh của user; mở project không tự ghi file;
- candidate và accepted được hiển thị **cạnh nhau**; pin và lý do stale hiển thị
  để user biết vì sao artifact không dùng được cho action phụ thuộc;
- ID (`vol_`/`arc_`) hiển thị kèm tiêu đề và phạm vi chương cho dễ đọc.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from novel_ai.core import validation
from novel_ai.core.models import ArtifactStatus, LongPlanPayload
from novel_ai.services import GuardError, ServiceError, long_planner
from novel_ai.ui import generation, page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = ["ACTIONS", "build_planning_scope", "describe_coverage", "render"]

ACTIONS: tuple[str, ...] = ("generate", "regenerate", "edit")

#: Artifact/workspace của page (dùng cho khóa editor và raw editor).
ARTIFACT_ID = "long_plan"
WORKSPACE = "long_plan"


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


def describe_coverage(
    payload: LongPlanPayload, scope: Mapping[str, int]
) -> list[str]:
    """Dòng preview coverage cho UI (hàm thuần, test được không cần Streamlit).

    Trả tổng horizon/số volume/số arc/số chương được phủ, phạm vi từng volume, và
    mọi issue cấu trúc mà validator sẽ dùng — nhờ vậy user thấy trước khi Accept.
    """
    arcs = [arc for volume in payload.volumes for arc in volume.arcs]
    covered = sum(arc.chapter_range.end - arc.chapter_range.start + 1 for arc in arcs)
    width = int(scope["end"]) - int(scope["start"]) + 1
    lines = [
        f"Horizon {scope['start']}–{scope['end']} ({width} chương) · "
        f"{len(payload.volumes)} volume · {len(arcs)} arc · {covered} chương được phủ"
    ]
    for volume in payload.volumes:
        ranges = ", ".join(
            f"`{arc.arc_id}` {arc.chapter_range.start}–{arc.chapter_range.end}"
            for arc in volume.arcs
        )
        lines.append(f"`{volume.volume_id}` {volume.title}: {ranges or 'không có arc'}")
    issues = validation.long_plan_horizon_issues(payload, scope)
    if not issues and covered == width:
        lines.append("Coverage liên tục và phủ đúng hai đầu horizon.")
    for issue in issues:
        lines.append(f"LỖI `{issue.code}` — {issue.message}")
    return lines


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


def _editor_prefix() -> str:
    """Prefix khóa session của form + raw editor candidate Long Plan."""
    return _common.editor_key(ARTIFACT_ID, workspace=WORKSPACE)


def _pending_edits_key(revision: Any) -> str:
    """Khóa session giữ các volume đang sửa dở theo **revision** đang xem.

    Revision nằm trong khóa để bản sửa dở của working copy cũ không bị merge vào
    candidate mới (regenerate trong lúc form đang mở).
    """
    return f"{_editor_prefix()}__pending_edits_r{int(revision)}"


def _reload_editors() -> None:
    """Quên hết giá trị form/raw editor để nạp lại từ candidate hiện tại."""
    _common.clear_session_keys_with_prefix(_editor_prefix())


def _pending_edits(revision: Any) -> dict[str, Any]:
    import streamlit as st

    stored = st.session_state.get(_pending_edits_key(revision)) or {}
    return {str(key): value for key, value in dict(stored).items()}


def _merged_payload(candidate: Any) -> dict[str, Any]:
    """Payload candidate + các volume đang sửa dở ở mọi vị trí đã render.

    Form chỉ render **một** volume tại một thời điểm; nếu chỉ gửi volume đang xem thì
    phần user đã sửa ở volume khác (chưa Save) sẽ mất. Vì vậy mọi volume đã render
    được giữ trong session state và merge lại khi Save.
    """
    payload_dict = candidate.payload.model_dump(mode="json")
    volumes = payload_dict.get("volumes") or []
    for position, edited in _pending_edits(candidate.revision).items():
        try:
            index = int(position)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(volumes):
            volumes[index] = edited
    payload_dict["volumes"] = volumes
    return payload_dict


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

    scoped = generation.scope_key(
        project_id=project.config.project_id,
        workspace="long_plan",
        artifact_id="long_plan",
    )
    surface = generation.start_surface(
        scoped, transcript=generation.load_transcript(scoped), project=project
    )

    _render_generate_form(ctx, surface=surface)
    _render_candidate(ctx, index)


def _render_generate_form(
    ctx: AppContext, *, surface: generation.GenerationSurface
) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Generate / Regenerate Long Plan")
    st.caption(
        "Guard backend: Base Idea accepted + Premise accepted và không stale. Volume/arc "
        "ID do backend reserve; ID tạm `tmp_volume_*`/`tmp_arc_*` được map sang stable ID."
    )
    st.caption(
        "`planning_scope` là **toàn bộ horizon cần kiến trúc** (có thể nhiều volume/arc hoặc "
        "toàn truyện), không phải số chương của một arc và không phải cửa sổ chỉnh sửa. App "
        "không suy horizon từ tiến độ viết."
    )
    stored_scope = long_planner.stored_planning_scope(project)
    if stored_scope is None:
        st.info(
            "Project chưa có horizon nào được lưu. Nhập `start`/`end` của toàn bộ phạm vi cần "
            "kiến trúc trước khi generate; đây là quyết định của bạn, không phải giá trị mặc định."
        )
    with st.form("novel_ai_long_plan_generate"):
        action = st.selectbox("Action", ACTIONS, key="novel_ai_long_plan_action")
        col_start, col_end = st.columns(2)
        with col_start:
            scope_start = st.number_input(
                "planning_scope.start",
                min_value=1,
                value=int(stored_scope["start"]) if stored_scope else None,
                step=1,
                key="novel_ai_long_plan_scope_start",
            )
        with col_end:
            scope_end = st.number_input(
                "planning_scope.end",
                min_value=1,
                value=int(stored_scope["end"]) if stored_scope else None,
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
        can_stream = _chapter_ui.stream_supported(ctx.llm_client)
        stream = st.checkbox(
            "Stream (hiện raw JSON theo từng delta)",
            value=bool(can_stream),
            key="novel_ai_long_plan_stream",
            disabled=not can_stream,
            help="Structured JSON chỉ được parse/validate sau khi stream hoàn tất.",
        )
        submitted = st.form_submit_button("Chạy generate/regenerate")
    if stored_scope is not None:
        st.caption(
            f"Horizon đã lưu trên revision hiện có: {stored_scope['start']}–"
            f"{stored_scope['end']}. Đổi giá trị ở trên là hành động tường minh; accepted cũ "
            "giữ nguyên cho tới khi candidate mới được accept."
        )
    if not submitted:
        return
    if scope_start is None or scope_end is None:
        st.error(
            "Cần nhập đủ `planning_scope.start` và `planning_scope.end` (horizon cần kiến "
            "trúc) trước khi generate; app không tự chọn hộ."
        )
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
    transcript, on_event = generation.recorder_for(
        surface,
        action=f"long_plan.{action}",
        operation_id=operation_id,
        stream=bool(stream),
        attempt=int(attempt),
        artifact_id="long_plan",
        project=project,
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
                on_event=on_event,
                stream=bool(stream),
                attempt=int(attempt),
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
        surface.render(transcript, project=project)
        return
    if blocked:
        surface.render(transcript, project=project)
        return
    surface.render(transcript, project=project)
    set_action_result(result)
    st.rerun()


def _render_candidate_form(project: Any, candidate: Any) -> None:
    """Editor Volume/Arc cho candidate Long Plan (T38, UI-03).

    Chọn Volume bằng selectbox, các Arc của volume đó render thành **card** (không
    expander lồng nhau). Save gọi `long_planner.edit_candidate` (T36) nên horizon,
    coverage, ID/FK và freshness dùng đúng guard backend; Save **không** accept.
    """
    import streamlit as st

    from novel_ai.core.models import VolumePlan
    from novel_ai.ui import editor

    payload_dict = candidate.payload.model_dump(mode="json")
    volumes = payload_dict.get("volumes") or []
    if not volumes:
        st.warning("Candidate chưa có volume nào; regenerate thay vì sửa tay từ rỗng.")
        return
    scope = candidate.planning_scope
    if scope is None:
        st.warning(
            "Candidate này là legacy (không có `planning_scope`); cần xác nhận horizon trước "
            "khi sửa."
        )
        return
    st.caption(
        f"Horizon của candidate (read-only, app quản lý): {scope.start}–{scope.end} · "
        f"accepted cũ chỉ đổi khi bạn bấm Accept."
    )
    index = _common.build_id_index(project)
    labels = {
        position: f"{volume.get('volume_id')} — {volume.get('title') or '(chưa có tiêu đề)'}"
        for position, volume in enumerate(volumes)
    }
    position = st.selectbox(
        "Volume đang sửa",
        list(labels),
        format_func=lambda value: labels.get(value, str(value)),
        key=f"novel_ai_long_plan_editor_volume_{candidate.revision}",
    )
    position = int(position)
    editor_key = f"{_editor_prefix()}__form_v{candidate.revision}_{position}"
    edited_volume = editor.render_model_form(
        VolumePlan, volumes[position], key_prefix=editor_key, depth=1
    )
    # Giữ bản đang sửa của volume này để Save không nuốt thay đổi ở volume khác.
    pending = _pending_edits(candidate.revision)
    pending[str(position)] = edited_volume
    st.session_state[_pending_edits_key(candidate.revision)] = pending
    st.caption(
        "Arc là card trong volume đang chọn; volume/arc khác được giữ nguyên khi Save. "
        "`chapter_range` sửa được trong card; ID arc mới phải do `long_planner.assign_ids` cấp."
    )
    if st.button("Lưu candidate (form)", key="novel_ai_long_plan_edit_form"):
        payload_to_save = _merged_payload(candidate)
        try:
            result = long_planner.edit_candidate(
                project,
                payload=payload_to_save,
                expected_revision=candidate.revision,
                operation_id=_chapter_ui.stable_operation_id(
                    "long_plan",
                    "edit_candidate",
                    candidate.revision,
                    json.dumps(payload_to_save, sort_keys=True, ensure_ascii=False),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Sửa đúng field/card được nêu ở trên rồi lưu lại; accepted Long Plan giữ nguyên."
                ),
            )
        else:
            set_action_result(result)
            # Nạp lại form/raw từ candidate mới; bản sửa vừa lưu không còn là "dở".
            _reload_editors()
            st.rerun()


def _render_candidate_raw(project: Any, candidate: Any) -> None:
    """Raw JSON cho candidate Long Plan, dùng **cùng** guard backend (T38).

    Text raw đi qua `long_planner.edit_candidate` nên schema/coverage/horizon/ID/FK
    và `planning_scope` (app-owned, không nằm trong payload) giữ nguyên luật; nội
    dung đang sửa không bị mất khi Save lỗi.
    """
    import streamlit as st

    key = _common.editor_key(ARTIFACT_ID, workspace=WORKSPACE, variant="raw")
    version = (candidate.revision, _common.payload_fingerprint(candidate.payload))
    _common.sync_text_editor(
        key, _common.payload_json_text(candidate.payload), version=version
    )
    st.caption(
        "Raw JSON đi qua đúng `long_planner.edit_candidate`: schema, coverage/horizon, ID/FK. "
        "`planning_scope`, revision và pin do app sở hữu nên không sửa được từ đây. Muốn horizon "
        "khác: xác nhận/regenerate ở phần generate phía trên."
    )
    edited = st.text_area(
        "Sửa candidate (JSON, cùng guard backend)", height=320, key=key
    )
    if st.button("Lưu candidate (raw JSON)", key="novel_ai_long_plan_edit_raw"):
        payload, parse_error = _common.parse_json_payload(edited)
        if parse_error:
            st.error(f"Chưa lưu được candidate: {parse_error}")
            st.info(
                "Bước tiếp theo: sửa JSON trong editor (nội dung bạn nhập vẫn còn) rồi lưu lại."
            )
            return
        try:
            result = long_planner.edit_candidate(
                project,
                payload=payload,
                expected_revision=candidate.revision,
                operation_id=_chapter_ui.stable_operation_id(
                    "long_plan",
                    "edit_candidate_raw",
                    candidate.revision,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Sửa JSON theo lỗi ở trên rồi lưu lại; accepted Long Plan giữ nguyên."
                ),
            )
        else:
            set_action_result(result)
            _reload_editors()
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
        scope = candidate.planning_scope
        if scope is None:
            st.warning(
                "Candidate này là **legacy**: không có `planning_scope` nên Accept bị chặn. "
                "Regenerate với horizon tường minh rồi accept lại."
            )
        else:
            scope_dict = {"start": int(scope.start), "end": int(scope.end)}
            st.caption(
                f"`planning_scope` của candidate: {scope.start}–{scope.end} "
                "(metadata app-owned; form không sửa được)"
            )
            st.markdown("**Preview coverage** (validator chạy cùng luật này)")
            for line in describe_coverage(candidate.payload, scope_dict):
                st.markdown(f"- {line}")
            warning = long_planner.single_arc_warning(candidate.payload)
            if warning:
                st.warning(warning)
        tabs = st.tabs(
            ["Editor (form)", "Candidate (dễ đọc)", "Accepted (dễ đọc)", "JSON thô (sửa)"]
        )
        if st.button(
            "Nạp lại editor từ candidate (bỏ thay đổi chưa Save)",
            key="novel_ai_long_plan_reload_editor",
        ):
            _reload_editors()
            st.rerun()
        with tabs[0]:
            _render_candidate_form(project, candidate)
        with tabs[1]:
            st.markdown("\n".join(_plan_lines(candidate.payload, index)))
        with tabs[2]:
            if accepted is None:
                st.caption("Chưa có accepted Long Plan.")
            else:
                st.markdown("\n".join(_plan_lines(accepted.payload, index)))
        with tabs[3]:
            _render_candidate_raw(project, candidate)
            if accepted is not None:
                with st.expander("Accepted (JSON, read-only)"):
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
        return
    st.caption(
        _common.selected_revision_text(accepted_envelope, which="accepted")
    )
    accepted_scope = accepted_envelope.accepted_revision.planning_scope
    if accepted_scope is not None:
        st.caption(
            f"`planning_scope` của accepted: {accepted_scope.start}–{accepted_scope.end}"
        )
        return
    _render_confirm_scope_form(ctx, accepted_envelope)


def _render_confirm_scope_form(ctx: AppContext, envelope: Any) -> None:
    """Action tường minh để xác nhận horizon cho accepted revision legacy (D017).

    Chỉ chạy khi user bấm nút: không auto migrate khi mở project, không sửa
    payload/manuscript, có snapshot trước khi ghi và no-op khi đã có scope.
    """
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.warning(
        "Long Plan accepted đang là **legacy**: chưa có `planning_scope`, nên coverage "
        "không thể được kiểm lại và generate/accept tiếp theo bị chặn. Xác nhận horizon "
        "thật của plan này nếu bạn muốn tiếp tục dùng nó."
    )
    st.caption(
        "App không lấy min/max arc hiện có làm horizon gốc, và không tự migrate khi mở "
        "project. Xác nhận horizon không đổi `volumes`/`arcs`."
    )
    with st.form("novel_ai_long_plan_confirm_scope"):
        col_start, col_end = st.columns(2)
        with col_start:
            start = st.number_input(
                "horizon.start", min_value=1, value=None, step=1,
                key="novel_ai_long_plan_confirm_start",
            )
        with col_end:
            end = st.number_input(
                "horizon.end", min_value=1, value=None, step=1,
                key="novel_ai_long_plan_confirm_end",
            )
        submitted = st.form_submit_button("Xác nhận horizon cho accepted revision")
    if not submitted:
        return
    if start is None or end is None:
        st.error("Cần nhập đủ `start` và `end` của horizon đã xác nhận.")
        return
    try:
        result = long_planner.confirm_planning_scope(
            project, start=int(start), end=int(end)
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu horizon xác nhận không khớp coverage hiện có: regenerate plan cho "
                "horizon đó thay vì migrate. Accepted giữ nguyên khi action lỗi."
            ),
        )
        return
    set_action_result(result)
    st.rerun()

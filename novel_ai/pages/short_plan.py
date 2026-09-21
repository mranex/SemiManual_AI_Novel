"""Workspace Short Plan: Arc → Chapter plan và Rolling Plan review (T20).

Page này hiện thực `docs/design/workflow.md` mục 4.2, 5.4 và
`novel_ai_spec_v0.2.md` mục 35, 22. Hai phần chính:

1. **Short Plan**: chọn arc, xem chapter được giao (`short_planner.assign_chapter_ids`
   hoặc `short_planner.resolve_assigned_chapters`), nhập `chapter_constraints`
   (`language`/`pov`/`length_guidance`), generate/accept/reject candidate.
2. **Rolling Plan**: hiển thị `eligible_chapters_for_rolling`, deviations và
   **phạm vi future changes** trước khi apply; apply chỉ đụng future Short Plan.

Luật đã giữ trong page:

- mọi mutation (`short_planner.generate`, `accept`, `reject`,
  `generate_rolling`, `accept_rolling`, `reject_rolling`) nằm sau
  `st.button`/`st.form_submit_button` trong nhánh `if submitted:`; rerun thuần
  không gọi LLM và không ghi file;
- chapter ID do backend cấp, UI chỉ hiển thị và gửi lại;
- reference ID (chapter/character/world rule/foreshadow) hiển thị kèm nhãn dễ đọc;
- UI nói rõ `allow_relationship_replan` và rằng apply Rolling chỉ đụng future
  Short Plan, không sửa quá khứ/final manuscript.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from novel_ai.core.models import ArtifactStatus, RollingPatchPayload
from novel_ai.services import GuardError, ServiceError, short_planner
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = ["ACTIONS", "build_chapter_constraint_values", "render"]

ACTIONS: tuple[str, ...] = ("generate", "regenerate", "edit")

KEY_ASSIGNED = "novel_ai_short_plan_assigned"


def assigned_cache_key(arc_id: str, long_plan_revision: int) -> str:
    """Khóa cache `assigned_chapters`: theo arc **và** revision Long Plan accepted.

    Hàm thuần để test được: sau khi revise Long Plan, revision đổi nên cache cũ
    không còn được dùng (C2 của `docs/design/review-findings-t24.md`).
    """
    return f"{KEY_ASSIGNED}_{arc_id}_r{int(long_plan_revision)}"

_CONSTRAINT_FIELDS: tuple[str, ...] = ("language", "pov", "length_guidance")


def _constraint_key(chapter_id: str, field: str) -> str:
    return f"novel_ai_short_plan_constraint_{chapter_id}_{field}"


def build_chapter_constraint_values(
    assigned_chapters: Sequence[Mapping[str, Any]],
    session_values: Mapping[str, Any],
    *,
    default_language: str,
) -> dict[str, dict[str, str]]:
    """Dựng `chapter_id -> {language, pov, length_guidance}` từ session state.

    Hàm thuần (không Streamlit) để test được: page chỉ truyền `st.session_state`
    vào. Ô nào trống thì nhận `default_language` của project ở field `language`;
    `pov`/`length_guidance` trống thì để trống (không tự bịa).
    """
    values: dict[str, dict[str, str]] = {}
    for item in assigned_chapters:
        chapter_id = str(item["chapter_id"])
        field_values: dict[str, str] = {}
        for field in _CONSTRAINT_FIELDS:
            raw = session_values.get(_constraint_key(chapter_id, field), "")
            field_values[field] = str(raw or "").strip()
        if not field_values["language"]:
            field_values["language"] = default_language
        values[chapter_id] = field_values
    return values


def _assigned_rows(ctx: AppContext, arc_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """Chapter được giao cho arc: dùng cache session, hoặc suy từ service.

    Trả `(rows, error)`. `error` là thông báo tiếng Việt khi service không cấp được
    chapter (hết chapter trong arc) — page hiển thị thay vì crash.

    Cache mang **revision của Long Plan accepted** (C2 của
    `review-findings-t24.md`): nếu chỉ khóa theo `arc_id`, sau khi revise Long Plan
    (thêm/bớt chapter trong arc) UI vẫn gửi `assigned_chapters` cũ vào
    `short_planner.generate` và candidate sẽ trỏ sai chương.
    """
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, "long_plan")
    payload = (
        envelope.accepted_revision.payload
        if envelope is not None and envelope.accepted_revision is not None
        else None
    )
    plan_revision = (
        envelope.accepted_revision.revision
        if envelope is not None and envelope.accepted_revision is not None
        else 0
    )
    cache_key = assigned_cache_key(arc_id, plan_revision)
    cached = st.session_state.get(cache_key)
    if cached is not None:
        return [dict(item) for item in cached], None
    arc = None
    if payload is not None:
        for volume in payload.volumes:
            for item in volume.arcs:
                if item.arc_id == arc_id:
                    arc = item
    if arc is None:
        return [], f"Arc `{arc_id}` không có trong Long Plan accepted."
    try:
        rows = short_planner.resolve_assigned_chapters(project, arc)
    except ServiceError as error:
        return [], str(error)
    rows = _common.assigned_chapter_rows(rows)
    rows.sort(key=lambda item: int(item["chapter_number"]))
    st.session_state[cache_key] = rows
    return rows, None


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Short Plan",
        "Arc → Chapter. Plan mô tả ý định tương lai; timeline/relationship không đổi.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Short Plan.")
        return

    options = _common.arc_options(project)
    if not options:
        st.warning(
            "Chưa có Long Plan accepted nên chưa lập Short Plan được. "
            "Bước tiếp theo: sang workspace Long Plan, generate rồi accept."
        )
        return

    labels = {item["arc_id"]: item["label"] for item in options}
    arc_id = st.selectbox(
        "Arc được lập Short Plan",
        list(labels),
        format_func=lambda key: labels.get(key, key),
        key="novel_ai_short_plan_arc",
    )
    arc_id = str(arc_id)
    selected = next(item for item in options if item["arc_id"] == arc_id)
    st.caption(
        f"Arc `{arc_id}` nằm trong volume `{selected['volume_id']}` · "
        f"phạm vi chương {selected['chapter_start']}–{selected['chapter_end']}."
    )
    _render_rolling(ctx, arc_id, selected)
    st.divider()
    _render_short_plan(ctx, arc_id, selected)


def _render_short_plan(ctx: AppContext, arc_id: str, selected: Mapping[str, Any]) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    index = _common.build_id_index(project)
    envelope = _common.envelope_or_none(project, "short_plan")
    st.subheader("Short Plan")
    st.caption(_common.artifact_caption(envelope, label="short_plan"))
    if envelope is not None and envelope.accepted_revision is not None:
        accepted_payload = envelope.accepted_revision.payload
        if accepted_payload.arc_id != arc_id:
            st.info(
                f"Short Plan accepted hiện thuộc arc `{accepted_payload.arc_id}`; "
                f"candidate mới cho arc `{arc_id}` sẽ được ghép theo `chapter_id` khi accept."
            )
    for note in _common.artifact_stale_notes(envelope):
        st.warning(note)

    rows, assign_error = _assigned_rows(ctx, arc_id)
    if assign_error:
        st.warning(assign_error)
    if not rows:
        return

    st.markdown("**Chapter được giao** (ID do backend cấp, không sửa tay ở đây)")
    for row in rows:
        st.markdown(
            "- " + _common.format_chapter_option(
                row["chapter_id"], row["chapter_number"], title=row.get("title", "")
            )
        )
    if selected["chapter_end"] - selected["chapter_start"] + 1 != len(rows):
        st.caption(
            "Một phần chapter trong arc đã có plan hoặc đã final nên không được giao lại."
        )

    default_language = project.config.default_language
    st.markdown("**`chapter_constraints`** — yêu cầu viết gửi cho LLM")
    values: dict[str, dict[str, str]] = {}
    for row in rows:
        with st.expander(
            f"Chương {row['chapter_number']} · `{row['chapter_id']}`", expanded=len(rows) <= 2
        ):
            language = st.text_input(
                "language",
                value=default_language,
                key=_constraint_key(row["chapter_id"], "language"),
            )
            pov = st.text_input(
                "pov (ví dụ: ngôi ba giới hạn theo Sở Dương)",
                key=_constraint_key(row["chapter_id"], "pov"),
            )
            length_guidance = st.text_input(
                "length_guidance (ví dụ: 1500–2000 từ)",
                key=_constraint_key(row["chapter_id"], "length_guidance"),
            )
            values[row["chapter_id"]] = {
                "language": language,
                "pov": pov,
                "length_guidance": length_guidance,
            }

    with st.form(f"novel_ai_short_plan_generate_{arc_id}"):
        action = st.selectbox("Action", ACTIONS, key="novel_ai_short_plan_action")
        instruction = st.text_area(
            "Yêu cầu thêm (tuỳ chọn)", key="novel_ai_short_plan_instruction", height=80
        )
        attempt = st.number_input(
            _chapter_ui.ATTEMPT_LABEL,
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            key="novel_ai_short_plan_attempt",
        )
        submitted = st.form_submit_button("Chạy generate/regenerate")
    if submitted:
        _run_generate(ctx, arc_id, rows, values, action, instruction, attempt)

    _render_candidate(ctx, arc_id, index)

    with st.expander("Xem `eligible_chapters_for_rolling` và pin"):
        eligible = short_planner.eligible_chapters_for_rolling(project)
        if not eligible:
            st.caption(
                "Chưa có chapter nào đủ điều kiện Rolling (cần Short Plan accepted và "
                "chapter nằm sau `latest_consistent_chapter`)."
            )
        else:
            for item in eligible:
                st.markdown(
                    "- " + _common.format_chapter_option(item["chapter_id"], item["chapter_number"])
                )
        timeline = _common.envelope_or_none(project, "short_plan")
        if timeline is not None and timeline.accepted_revision is not None:
            for pin in timeline.accepted_revision.dependency_pins:
                st.caption(f"pin `{pin.artifact_id}` r{pin.revision}")


def _run_generate(
    ctx: AppContext,
    arc_id: str,
    rows: Sequence[Mapping[str, Any]],
    values: Mapping[str, Mapping[str, Any]],
    action: Any,
    instruction: Any,
    attempt: Any = 1,
) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    if ctx.llm_client is None:
        st.error(
            "Chưa dựng được LLM client nên không gọi được `short_planner.generate`."
            + (f" {ctx.llm_error}" if ctx.llm_error else "")
        )
        return
    constraints = _common.build_chapter_constraints(
        rows, values, default_language=project.config.default_language
    )
    st.caption(
        "Yêu cầu viết sẽ gửi: " + " | ".join(_common.chapter_constraints_summary(constraints))
    )
    constraints_payload = _common.chapter_constraints_payload(constraints)
    operation_id = _chapter_ui.stable_operation_id(
        "short_plan",
        str(action),
        arc_id,
        str(instruction or "").strip(),
        int(attempt),
        ",".join(str(item["chapter_id"]) for item in rows),
        json.dumps(constraints_payload, ensure_ascii=False, sort_keys=True),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            f"novel_ai_short_plan_generate_{arc_id}",
            intent=("generate", arc_id, operation_id),
            project=project,
            call=lambda: short_planner.generate(
                project,
                client=ctx.llm_client,
                arc_id=arc_id,
                assigned_chapters=rows,
                chapter_constraints=constraints_payload,
                action=str(action),
                user_instruction=str(instruction or "").strip(),
                operation_id=operation_id,
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu là guard/scope: kiểm tra arc và chapter được giao. Nếu là validation: "
                "xem field sai rồi regenerate. Short Plan accepted cũ giữ nguyên."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _render_candidate(ctx: AppContext, arc_id: str, index: Mapping[str, str]) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, "short_plan")
    st.markdown("**Candidate vs accepted**")
    if envelope is None or envelope.candidate_revision is None:
        st.caption("Chưa có candidate Short Plan nào đang chờ accept.")
    else:
        candidate = envelope.candidate_revision
        accepted = envelope.accepted_revision
        st.caption(_common.candidate_update_hint_for_envelope(envelope))
        st.caption(
            f"Candidate r{candidate.revision} · validation `{candidate.validation.state.value}`"
            + (f" · accepted r{accepted.revision}" if accepted is not None else "")
        )
        for issue in candidate.validation.errors:
            st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")
        tabs = st.tabs(["Candidate (dễ đọc)", "Accepted (dễ đọc)", "JSON thô"])
        with tabs[0]:
            st.markdown(
                "\n".join(_common.short_plan_reference_lines(candidate.payload, index))
            )
        with tabs[1]:
            if accepted is None:
                st.caption("Chưa có Short Plan accepted.")
            else:
                st.markdown(
                    "\n".join(_common.short_plan_reference_lines(accepted.payload, index))
                )
        with tabs[2]:
            st.json(candidate.payload.model_dump(mode="json"))
        accept_col, reject_col = st.columns(2)
        with accept_col:
            accept_clicked = st.button("Accept Short Plan", key="novel_ai_short_plan_accept")
        with reject_col:
            reject_clicked = st.button("Reject candidate", key="novel_ai_short_plan_reject")
        if accept_clicked:
            try:
                result = short_planner.accept(project)
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Regenerate nếu pin lệch/scope sai. Accept sẽ tạo chapter metadata "
                        "`planned` trong cùng transaction."
                    ),
                )
            else:
                set_action_result(result)
                st.rerun()
        if reject_clicked:
            try:
                result = short_planner.reject(project)
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
            "Chưa có Short Plan accepted. Bước tiếp theo: generate rồi accept để Skeleton "
            "của các chapter `planned` được mở."
        )
    else:
        st.caption(_common.selected_revision_text(accepted_envelope, which="accepted"))
    if accepted_envelope is not None and accepted_envelope.accepted_revision is not None:
        st.caption("Chapter metadata hiện có:")
        for chapter_id in _chapter_ids(accepted_envelope.accepted_revision.payload):
            st.markdown(f"- `{chapter_id}`")


def _chapter_ids(payload: Any) -> list[str]:
    return [chapter.chapter_id for chapter in getattr(payload, "chapters", []) or []]


# ---------------------------------------------------------------------------
# Rolling Plan
# ---------------------------------------------------------------------------


def render_rolling_patch_caption(
    patch: RollingPatchPayload,
    *,
    eligible: Sequence[Mapping[str, Any]],
    allow_relationship_replan: bool,
) -> _common.RollingView:
    """Dựng view Rolling để hiển thị trước khi apply (bọc `_common.build_rolling_view`)."""
    return _common.build_rolling_view(
        patch, eligible_chapters=eligible, allow_relationship_replan=allow_relationship_replan
    )


def _render_rolling(ctx: AppContext, arc_id: str, selected: Mapping[str, Any]) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    index = _common.build_id_index(project)
    st.subheader("Rolling Plan")
    st.caption(
        "Rolling Plan đọc actual gần nhất rồi đề xuất chỉnh **future** Short Plan. "
        "Apply không sửa Base Idea/Premise/Long Plan, không sửa final manuscript và "
        "không sửa chapter đã final."
    )
    eligible = short_planner.eligible_chapters_for_rolling(project)
    if eligible:
        st.markdown("**`eligible_chapters_for_rolling`** (chỉ những chapter này được đề xuất):")
        for item in eligible:
            st.markdown(
                "- " + _common.format_chapter_option(item["chapter_id"], item["chapter_number"])
            )
    else:
        st.info(
            "Chưa có chapter nào đủ điều kiện Rolling. Rolling cần Short Plan accepted và "
            "đã có chapter `final_reconciled` làm actual."
        )
    allow_replan = bool(project.config.allow_relationship_replan)
    st.caption(
        f"`allow_relationship_replan` = **{'true' if allow_replan else 'false'}** "
        "(`project.config.allow_relationship_replan`). "
        + (
            "Rolling được đề xuất thay đổi hướng quan hệ tương lai."
            if allow_replan
            else "Rolling bị từ chối mọi đề xuất đổi ý định quan hệ, kể cả gián tiếp qua "
            "summary/outline/threads/hook."
        )
    )
    st.caption(
        "Apply Rolling chỉ đụng **future Short Plan** (chapter trong eligible scope) và "
        "chapter metadata tương ứng; chapter đã `finalizing`/`final_reconciled` bị từ chối."
    )

    with st.form(f"novel_ai_rolling_generate_{arc_id}"):
        user_instruction = st.text_area(
            "Yêu cầu cho lần Rolling review (tuỳ chọn)",
            key=f"novel_ai_rolling_instruction_{arc_id}",
            height=80,
        )
        submitted = st.form_submit_button("Generate Rolling proposal")
    if submitted:
        if ctx.llm_client is None:
            st.error(
                "Chưa dựng được LLM client nên không gọi được `short_planner.generate_rolling`."
                + (f" {ctx.llm_error}" if ctx.llm_error else "")
            )
        else:
            try:
                result = short_planner.generate_rolling(
                    project,
                    client=ctx.llm_client,
                    arc_id=arc_id,
                    user_instruction=str(user_instruction or "").strip(),
                )
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Rolling cần ít nhất một chapter đã final_reconciled làm actual; "
                        "nếu chưa có, tiếp tục viết/finalize chapter trước."
                    ),
                )
            else:
                set_action_result(result)
                st.rerun()

    _render_rolling_candidate(ctx, arc_id, eligible, index, allow_replan)


def _render_rolling_candidate(
    ctx: AppContext,
    arc_id: str,
    eligible: Sequence[Mapping[str, Any]],
    index: Mapping[str, str],
    allow_replan: bool,
) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    artifact_id = f"rolling_patch_{arc_id}"
    envelope = _common.envelope_or_none(project, artifact_id)
    st.markdown("**Proposal đang chờ apply**")
    if envelope is None or (
        envelope.candidate_revision is None and envelope.accepted_revision is None
    ):
        st.caption(f"Chưa có Rolling proposal `{artifact_id}`.")
        return
    if envelope.candidate_revision is None:
        st.caption(
            f"`{artifact_id}` không có candidate mới; proposal đã được apply/reject trước đó."
        )
        if envelope.accepted_revision is not None:
            st.json(envelope.accepted_revision.payload.model_dump(mode="json"))
        return

    candidate = envelope.candidate_revision
    view = render_rolling_patch_caption(
        candidate.payload, eligible=eligible, allow_relationship_replan=allow_replan
    )
    st.caption(
        f"`{artifact_id}` status `{view.proposal_status}` · {view.summary()} · "
        + (
            f"reviewed range {view.reviewed_range[0]}–{view.reviewed_range[1]}"
            if view.reviewed_range
            else "chưa có reviewed range"
        )
    )
    st.markdown("**Phạm vi thay đổi trước khi apply**")
    if view.short_plan_changes or view.relationship_changes:
        for change in view.short_plan_changes:
            st.markdown("- " + change.as_label(index))
            st.caption("  " + _common.compact_json(change.payload))
        for change in view.relationship_changes:
            st.markdown("- Hướng quan hệ: " + change.as_label(index))
            st.caption("  " + _common.compact_json(change.payload))
    else:
        st.caption("Proposal không đổi field nào của Short Plan.")
    if view.touches_only_future:
        st.success(
            "Mọi target nằm trong eligible scope: apply chỉ đụng future Short Plan."
        )
    else:
        st.error(
            "Target ngoài eligible scope: "
            + ", ".join(f"`{item}`" for item in view.out_of_scope_targets)
            + ". Backend sẽ từ chối apply (không apply một phần)."
        )
    if view.deviations:
        st.markdown("**Deviations (planned vs actual)**")
        for deviation in view.deviations:
            st.markdown(
                f"- `{deviation.get('chapter_id')}` · planned: {deviation.get('planned')} · "
                f"actual: {deviation.get('actual')} · evidence: {deviation.get('evidence')}"
            )
    else:
        st.caption("Không ghi nhận deviation nào.")
    if view.blocked_by_authority:
        st.warning("Proposal có mục cần **bạn quyết định** (không tự apply):")
        for blocked in view.blocked_by_authority:
            st.markdown(
                f"- authority `{blocked.get('authority')}` · "
                f"{blocked.get('reason')} (chapter `{blocked.get('chapter_id')}`)"
            )
    with st.expander("JSON proposal"):
        st.json(candidate.payload.model_dump(mode="json"))
    for issue in candidate.validation.errors:
        st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")

    accept_col, reject_col = st.columns(2)
    with accept_col:
        accept_clicked = st.button(
            "Apply Rolling vào Short Plan", key=f"novel_ai_rolling_accept_{arc_id}"
        )
    with reject_col:
        reject_clicked = st.button(
            "Reject Rolling proposal", key=f"novel_ai_rolling_reject_{arc_id}"
        )
    if accept_clicked:
        try:
            result = short_planner.accept_rolling(project)
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Regenerate proposal nếu pin lệch hoặc target ngoài eligible; "
                    "Short Plan accepted giữ nguyên nếu apply lỗi."
                ),
            )
        else:
            set_action_result(result)
            st.rerun()
    if reject_clicked:
        try:
            result = short_planner.reject_rolling(project)
        except ServiceError as error:
            _common.render_service_error(
                error, next_step="Kiểm tra lại state; proposal có thể đã bị reject."
            )
        else:
            set_action_result(result)
            st.rerun()

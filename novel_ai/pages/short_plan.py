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
from novel_ai.ui import generation, page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = ["ACTIONS", "build_chapter_constraint_values", "render"]

ACTIONS: tuple[str, ...] = ("generate", "regenerate", "edit")

KEY_ASSIGNED = "novel_ai_short_plan_assigned"

#: Artifact/workspace của page (dùng cho khóa editor và raw editor).
ARTIFACT_ID = "short_plan"
WORKSPACE = "short_plan"


def _editor_prefix() -> str:
    """Prefix khóa session của form + raw editor candidate Short Plan."""
    return _common.editor_key(ARTIFACT_ID, workspace=WORKSPACE)


def _pending_edits_key(revision: Any) -> str:
    """Khóa session giữ các chapter đang sửa dở theo **revision** đang xem.

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
    """Payload candidate + các chapter đang sửa dở ở mọi vị trí đã render.

    Form chỉ render **một** chapter tại một thời điểm; merge lại để Save không nuốt
    thay đổi chưa Save của chapter khác. Chapter ngoài arc vẫn do service ghép từ
    accepted (`short_planner._merge_with_accepted`).
    """
    payload_dict = candidate.payload.model_dump(mode="json")
    chapters = payload_dict.get("chapters") or []
    for position, edited in _pending_edits(candidate.revision).items():
        try:
            index = int(position)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(chapters):
            chapters[index] = edited
    payload_dict["chapters"] = chapters
    return payload_dict


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
    default_pov: str = "",
    default_length_guidance: str = "",
) -> dict[str, dict[str, str]]:
    """Dựng `chapter_id -> {language, pov, length_guidance}` từ session state.

    Hàm thuần (không Streamlit) để test được: page chỉ truyền `st.session_state`
    vào. Ô nào trống thì nhận default của project (D016); default rỗng vẫn để rỗng
    để guard backend báo thiếu thay vì app tự bịa POV/độ dài.
    """
    defaults = {
        "language": str(default_language or "").strip(),
        "pov": str(default_pov or "").strip(),
        "length_guidance": str(default_length_guidance or "").strip(),
    }
    values: dict[str, dict[str, str]] = {}
    for item in assigned_chapters:
        chapter_id = str(item["chapter_id"])
        field_values: dict[str, str] = {}
        for field in _CONSTRAINT_FIELDS:
            raw = session_values.get(_constraint_key(chapter_id, field), "")
            field_values[field] = str(raw or "").strip() or defaults[field]
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
    scoped = generation.scope_key(
        project_id=project.config.project_id,
        workspace="short_plan",
        artifact_id=f"short_plan_{arc_id}",
    )
    surface = generation.start_surface(
        scoped, transcript=generation.load_transcript(scoped), project=project
    )
    _render_rolling(ctx, arc_id, selected)
    st.divider()
    _render_short_plan(ctx, arc_id, selected, surface=surface)


def _render_short_plan(
    ctx: AppContext,
    arc_id: str,
    selected: Mapping[str, Any],
    *,
    surface: generation.GenerationSurface,
) -> None:
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
        # Không còn chapter nào để lập mới **không** có nghĩa là hết việc: candidate Short
        # Plan hiện có vẫn phải xem/sửa/accept được, nếu không user phải accept mù hoặc mở
        # raw JSON (UI-03). Trước đây page `return` sớm nên panel candidate không render.
        st.info(
            "Arc này không còn chapter nào để lập Short Plan **mới** (mọi chapter trong arc "
            "đã có plan hoặc đã final). Candidate Short Plan hiện có vẫn xem/sửa/accept được "
            "ở phần dưới."
        )
        _render_candidate(ctx, arc_id, index)
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
    _render_writing_defaults(project)
    st.markdown("**`chapter_constraints`** — yêu cầu viết gửi cho LLM")
    st.caption(
        "Giá trị hiệu lực = override của chương nếu bạn nhập, ngược lại default viết của "
        "project. Guard backend chặn **trước khi gọi LLM** nếu bất kỳ chapter nào còn thiếu."
    )
    defaults = short_planner.project_writing_defaults(project)
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
                value=defaults["pov"],
                key=_constraint_key(row["chapter_id"], "pov"),
            )
            length_guidance = st.text_input(
                "length_guidance (ví dụ: 1500–2000 từ)",
                value=defaults["length_guidance"],
                key=_constraint_key(row["chapter_id"], "length_guidance"),
            )
            values[row["chapter_id"]] = {
                "language": language,
                "pov": pov,
                "length_guidance": length_guidance,
            }
    effective = short_planner.effective_chapter_constraints(
        project, rows, _constraint_payload(rows, values)
    )
    constraint_issues = short_planner.resolve_constraint_issues(
        project, rows, _constraint_payload(rows, values)
    )
    st.caption("**Giá trị hiệu lực sẽ gửi:**")
    for row in effective:
        st.markdown(
            f"- `{row['chapter_id']}` · language `{row['language'] or '—'}` · "
            f"pov `{row['pov'] or '—'}` · length `{row['length_guidance'] or '—'}`"
        )
    for issue in constraint_issues:
        label = f"`{issue['chapter_id']}`" if issue["chapter_id"] else "(thiếu chapter_id)"
        st.warning(f"{label} · `{issue['code']}` · {issue['message']}")

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
        can_stream = _chapter_ui.stream_supported(ctx.llm_client)
        stream = st.checkbox(
            "Stream (hiện raw JSON theo từng delta)",
            value=bool(can_stream),
            key="novel_ai_short_plan_stream",
            disabled=not can_stream,
            help="Structured JSON chỉ được parse/validate sau khi stream hoàn tất.",
        )
        submitted = st.form_submit_button("Chạy generate/regenerate")
    if submitted:
        _run_generate(
            ctx,
            arc_id,
            rows,
            values,
            action,
            instruction,
            attempt,
            stream=bool(stream),
            surface=surface,
        )

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


def _constraint_payload(
    rows: Sequence[Mapping[str, Any]], values: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, str]]:
    """`chapter_constraints` từ giá trị UI.

    Giữ cả field rỗng (không lọc bỏ entry) để guard backend báo đúng chapter/field
    còn thiếu, thay vì âm thầm gửi request thiếu contract.
    """
    payload: list[dict[str, str]] = []
    for row in rows:
        chapter_id = str(row["chapter_id"])
        row_values = values.get(chapter_id, {})
        entry = {"chapter_id": chapter_id}
        for field in _CONSTRAINT_FIELDS:
            entry[field] = str(row_values.get(field, "") or "").strip()
        payload.append(entry)
    return payload


def _render_writing_defaults(project: Any) -> None:
    """Default viết theo project: hiển thị, và Save bằng action tường minh (D016).

    Save chỉ ghi `project.json` (không gọi LLM, không tạo candidate, không sửa
    accepted plan). Project cũ thiếu field hiện giá trị rỗng kèm hướng dẫn.
    """
    import streamlit as st

    defaults = short_planner.project_writing_defaults(project)
    missing = [field for field in ("pov", "length_guidance") if not defaults[field]]
    st.markdown("**Default viết của project**")
    st.caption(
        "Default là input cho request Short Plan mới; đổi default **không** rewrite Short Plan "
        "đã accepted. Override từng chương ở dưới sẽ thắng default."
    )
    if missing:
        st.info(
            "Project chưa thiết lập default cho: " + ", ".join(f"`{field}`" for field in missing)
            + ". Nhập rồi bấm **Lưu default viết** (hoặc override từng chương) trước khi generate."
        )
    with st.form("novel_ai_writing_defaults"):
        st.text_input(
            "default_language (từ project, sửa ở project config)",
            value=project.config.default_language,
            disabled=True,
            key="novel_ai_writing_default_language",
        )
        pov = st.text_input(
            "default_pov",
            value=defaults["pov"],
            key="novel_ai_writing_default_pov",
        )
        length_guidance = st.text_input(
            "default_length_guidance",
            value=defaults["length_guidance"],
            key="novel_ai_writing_default_length",
        )
        submitted = st.form_submit_button("Lưu default viết")
    if not submitted:
        return
    try:
        project.update_config(
            default_pov=str(pov or "").strip(),
            default_length_guidance=str(length_guidance or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 - báo lỗi thật của project config cho user
        st.error(f"Không lưu được default viết: {type(exc).__name__} — {exc}")
        return
    st.success("Đã lưu default viết vào `project.json` (không gọi LLM, không sửa accepted plan).")
    st.rerun()


def _run_generate(
    ctx: AppContext,
    arc_id: str,
    rows: Sequence[Mapping[str, Any]],
    values: Mapping[str, Mapping[str, Any]],
    action: Any,
    instruction: Any,
    attempt: Any = 1,
    *,
    stream: bool = False,
    surface: generation.GenerationSurface | None = None,
) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    constraints_payload = _constraint_payload(rows, values)
    effective = short_planner.effective_chapter_constraints(project, rows, constraints_payload)
    st.caption(
        "Yêu cầu viết sẽ gửi: "
        + " | ".join(
            f"`{row['chapter_id']}` {row['language']} · {row['pov']} · {row['length_guidance']}"
            for row in effective
        )
    )
    # Guard sớm ở UI cho phản hồi nhanh; service vẫn là guard thật và chạy cùng luật.
    issues = short_planner.resolve_constraint_issues(project, rows, constraints_payload)
    if issues:
        for issue in issues:
            label = f"`{issue['chapter_id']}`" if issue["chapter_id"] else "(thiếu chapter_id)"
            st.error(f"{label} · `{issue['code']}` · {issue['message']}")
        st.error(
            "Chưa đủ yêu cầu viết nên **không gọi LLM**: lưu default viết của project hoặc "
            "nhập override cho từng chương rồi chạy lại."
        )
        return
    if ctx.llm_client is None:
        st.error(
            "Chưa dựng được LLM client nên không gọi được `short_planner.generate`."
            + (f" {ctx.llm_error}" if ctx.llm_error else "")
        )
        return
    operation_id = _chapter_ui.stable_operation_id(
        "short_plan",
        str(action),
        arc_id,
        str(instruction or "").strip(),
        int(attempt),
        ",".join(str(item["chapter_id"]) for item in rows),
        json.dumps(constraints_payload, ensure_ascii=False, sort_keys=True),
    )
    on_event = None
    transcript = None
    if surface is not None:
        transcript, on_event = generation.recorder_for(
            surface,
            action=f"short_plan.{action}",
            operation_id=operation_id,
            stream=bool(stream),
            attempt=int(attempt),
            artifact_id=f"short_plan_{arc_id}",
            project=project,
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
                on_event=on_event,
                stream=bool(stream),
                attempt=int(attempt),
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
        if surface is not None and transcript is not None:
            surface.render(transcript, project=project)
        return
    if blocked:
        if surface is not None and transcript is not None:
            surface.render(transcript, project=project)
        return
    if surface is not None and transcript is not None:
        surface.render(transcript, project=project)
    set_action_result(result)
    st.rerun()


def _render_candidate_form(
    ctx: AppContext, candidate: Any, index: Mapping[str, str]
) -> None:
    """Editor Chapter Plan cho candidate Short Plan (T38, UI-03).

    Chọn chapter bằng selectbox; field của chapter đó render thành form đúng schema
    (ID selector lấy từ accepted index). Save gọi `short_planner.edit_candidate`
    (T36) nên scope arc, chapter locked, contract viết và FK dùng đúng guard backend;
    Save **không** accept và không ghi chapter metadata.
    """
    import streamlit as st

    from novel_ai.core.models import ChapterPlan
    from novel_ai.ui import editor

    project = ctx.project
    assert project is not None
    payload_dict = candidate.payload.model_dump(mode="json")
    chapters = payload_dict.get("chapters") or []
    if not chapters:
        st.warning("Candidate chưa có chapter nào; generate/regenerate thay vì sửa tay từ rỗng.")
        return
    st.caption(
        f"Arc `{candidate.payload.arc_id}` · candidate r{candidate.revision}. Chapter ngoài arc "
        "được giữ nguyên từ accepted; chapter đã final không sửa được ở đây."
    )
    defaults = short_planner.project_writing_defaults(project)
    effective = short_planner.effective_chapter_constraints(project, [
        {"chapter_id": chapter["chapter_id"], "chapter_number": chapter["chapter_number"]}
        for chapter in chapters
    ])
    effective_by_id = {row["chapter_id"]: row for row in effective}
    labels = {
        position: (
            f"Ch.{chapter.get('chapter_number')} · {chapter.get('chapter_id')} — "
            f"{chapter.get('title') or '(chưa có tiêu đề)'}"
        )
        for position, chapter in enumerate(chapters)
    }
    position = st.selectbox(
        "Chapter đang sửa",
        list(labels),
        format_func=lambda value: labels.get(value, str(value)),
        key=f"novel_ai_short_plan_editor_chapter_{candidate.revision}",
    )
    position = int(position)
    chapter_dict = chapters[position]
    chapter_id = str(chapter_dict.get("chapter_id", ""))
    resolved = effective_by_id.get(chapter_id)
    if resolved is not None:
        st.caption(
            "Contract viết **hiệu lực** (default project + override chương, T31): "
            f"language `{resolved['language'] or '—'}` · pov `{resolved['pov'] or '—'}` · "
            f"length `{resolved['length_guidance'] or '—'}` "
            f"(default project: pov `{defaults['pov'] or '—'}`, "
            f"length `{defaults['length_guidance'] or '—'}`)."
        )
    editor_key = f"{_editor_prefix()}__form_ch{candidate.revision}_{position}"
    edited_chapter = editor.render_model_form(
        ChapterPlan,
        chapter_dict,
        key_prefix=editor_key,
        depth=1,
        # T39: selector chỉ chứa ID đúng loại (char/rule/fs), lấy từ accepted foundation.
        selectors=_common.selector_options(project),
    )
    payload_dict["chapters"][position] = edited_chapter
    # Giữ bản đang sửa của chapter này để Save không nuốt thay đổi ở chapter khác.
    pending = _pending_edits(candidate.revision)
    pending[str(position)] = edited_chapter
    st.session_state[_pending_edits_key(candidate.revision)] = pending
    st.caption(
        "Chapter ngoài arc và chapter đã `final`/`finalizing` không sửa được ở đây "
        "(`chapter_id`/`chapter_number` do app cấp, hiển thị read-only)."
    )
    if st.button("Lưu candidate (form)", key="novel_ai_short_plan_edit_form"):
        payload_to_save = _merged_payload(candidate)
        try:
            result = short_planner.edit_candidate(
                project,
                arc_id=str(candidate.payload.arc_id),
                payload=payload_to_save,
                expected_revision=candidate.revision,
                operation_id=_chapter_ui.stable_operation_id(
                    "short_plan",
                    "edit_candidate",
                    candidate.revision,
                    json.dumps(payload_to_save, sort_keys=True, ensure_ascii=False),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Sửa đúng field/card được nêu ở trên rồi lưu lại; Short Plan accepted và "
                    "chapter metadata giữ nguyên."
                ),
            )
        else:
            set_action_result(result)
            # Nạp lại form/raw từ candidate mới; bản sửa vừa lưu không còn là "dở".
            _reload_editors()
            st.rerun()


def _render_candidate_raw(ctx: AppContext, candidate: Any) -> None:
    """Raw JSON cho candidate Short Plan, dùng **cùng** guard backend (T38).

    Text raw đi qua `short_planner.edit_candidate` nên arc/scope, chapter locked,
    contract viết và FK giữ nguyên luật; `chapter_id`/`chapter_number`/pin do app
    sở hữu (sửa số chương bị từ chối với `chapter_number_mismatch`).
    """
    import streamlit as st

    project = ctx.project
    assert project is not None
    arc_id = str(candidate.payload.arc_id)
    key = _common.editor_key(ARTIFACT_ID, workspace=WORKSPACE, variant="raw")
    version = (arc_id, candidate.revision, _common.payload_fingerprint(candidate.payload))
    _common.sync_text_editor(
        key, _common.payload_json_text(candidate.payload), version=version
    )
    st.caption(
        f"Raw JSON đi qua đúng `short_planner.edit_candidate` cho arc `{arc_id}`: schema, "
        "contract viết của mọi chapter, FK và chapter đã final. `chapter_id`/`chapter_number` "
        "và pin do app sở hữu."
    )
    edited = st.text_area(
        "Sửa candidate (JSON, cùng guard backend)", height=320, key=key
    )
    if st.button("Lưu candidate (raw JSON)", key="novel_ai_short_plan_edit_raw"):
        payload, parse_error = _common.parse_json_payload(edited)
        if parse_error:
            st.error(f"Chưa lưu được candidate: {parse_error}")
            st.info(
                "Bước tiếp theo: sửa JSON trong editor (nội dung bạn nhập vẫn còn) rồi lưu lại."
            )
            return
        try:
            result = short_planner.edit_candidate(
                project,
                arc_id=arc_id,
                payload=payload,
                expected_revision=candidate.revision,
                operation_id=_chapter_ui.stable_operation_id(
                    "short_plan",
                    "edit_candidate_raw",
                    candidate.revision,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Sửa JSON theo lỗi ở trên rồi lưu lại; Short Plan accepted và chapter "
                    "metadata giữ nguyên."
                ),
            )
        else:
            set_action_result(result)
            _reload_editors()
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
        tabs = st.tabs(
            ["Editor (form)", "Candidate (dễ đọc)", "Accepted (dễ đọc)", "JSON thô (sửa)"]
        )
        if st.button(
            "Nạp lại editor từ candidate (bỏ thay đổi chưa Save)",
            key="novel_ai_short_plan_reload_editor",
        ):
            _reload_editors()
            st.rerun()
        with tabs[0]:
            _render_candidate_form(ctx, candidate, index)
        with tabs[1]:
            st.markdown(
                "\n".join(_common.short_plan_reference_lines(candidate.payload, index))
            )
        with tabs[2]:
            if accepted is None:
                st.caption("Chưa có Short Plan accepted.")
            else:
                st.markdown(
                    "\n".join(_common.short_plan_reference_lines(accepted.payload, index))
                )
        with tabs[3]:
            _render_candidate_raw(ctx, candidate)
            if accepted is not None:
                with st.expander("Accepted (JSON, read-only)"):
                    st.json(accepted.payload.model_dump(mode="json"))
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
            operation_id = _chapter_ui.stable_operation_id(
                "rolling_plan", arc_id, str(user_instruction or "").strip()
            )
            rolling_scoped = generation.scope_key(
                project_id=project.config.project_id,
                workspace="short_plan",
                artifact_id=f"rolling_patch_{arc_id}",
            )
            rolling_surface = generation.start_surface(
                rolling_scoped,
                transcript=generation.load_transcript(rolling_scoped),
                project=project,
                title="AI Generation — Rolling Plan",
            )
            transcript, on_event = generation.recorder_for(
                rolling_surface,
                action="rolling_plan.generate",
                operation_id=operation_id,
                stream=False,
                artifact_id=f"rolling_patch_{arc_id}",
                project=project,
            )
            try:
                result = short_planner.generate_rolling(
                    project,
                    client=ctx.llm_client,
                    arc_id=arc_id,
                    user_instruction=str(user_instruction or "").strip(),
                    operation_id=operation_id,
                    on_event=on_event,
                )
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Rolling cần ít nhất một chapter đã final_reconciled làm actual; "
                        "nếu chưa có, tiếp tục viết/finalize chapter trước."
                    ),
                )
                rolling_surface.render(transcript, project=project)
            else:
                rolling_surface.render(transcript, project=project)
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

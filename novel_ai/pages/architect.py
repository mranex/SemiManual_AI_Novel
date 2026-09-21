"""Workspace Architect: generate/edit/accept/reject/append foundation (T20).

Page này hiện thực `docs/design/workflow.md` mục 4.1, 5.3, 5.4 và
`novel_ai_spec_v0.2.md` mục 33. Bốn artifact được quản lý: `premise`,
`characters`, `world_rules`, `foreshadow`.

Luật đã giữ trong page:

- mọi mutation (`architect.generate`, `edit_candidate`, `accept`, `reject`,
  `append_entries`) nằm sau `st.button`/`st.form_submit_button` trong nhánh
  `if submitted:`; rerun thuần không gọi LLM và không ghi file;
- `assigned_ids` lấy từ `architect.assign_ids` (backend reserve stable ID), không
  tự sinh ID trong UI;
- `effective_from_chapter` của append là input **rõ ràng** của user, không suy từ
  payload;
- candidate/JSON sai làm UI hiển thị lỗi theo field nhưng **không** mất accepted
  revision và không mất nội dung user đang sửa;
- UI nói rõ Auto Accept structured chỉ áp cho structured output, **không** liên
  quan prose/Human Review.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from novel_ai.core.models import ArtifactStatus
from novel_ai.services import GuardError, ServiceError, architect
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "APPEND_TYPES",
    "ASSIGNED_ID_PREFIXES",
    "build_architect_action",
    "parse_assigned_ids",
    "render",
]

#: Artifact mà workspace này quản lý, theo thứ tự authority.
ARTIFACT_TYPES: tuple[str, ...] = (
    "premise",
    "characters",
    "world_rules",
    "foreshadow",
)

#: Nhãn hiển thị cho từng artifact.
ARTIFACT_LABELS: dict[str, str] = {
    "premise": "Premise",
    "characters": "Characters",
    "world_rules": "World Rules",
    "foreshadow": "Foreshadow",
}

#: Artifact có entry appendable kèm `effective_from_chapter`.
APPEND_TYPES: tuple[str, ...] = ("characters", "world_rules", "foreshadow")

#: Prefix stable ID hợp lệ theo artifact (khớp `core.models.ID_PREFIXES`).
ASSIGNED_ID_PREFIXES: dict[str, str] = {
    "characters": "char",
    "world_rules": "rule",
    "foreshadow": "fs",
}

#: `artifact_type -> (collection field, id field)` cho entry appendable.
_ENTITY_FIELDS: dict[str, tuple[str, str]] = {
    "characters": ("characters", "character_id"),
    "world_rules": ("world_rules", "world_rule_id"),
    "foreshadow": ("foreshadows", "foreshadow_id"),
}

#: Action hợp lệ của `architect.generate`.
ACTIONS: tuple[str, ...] = ("generate", "regenerate", "edit")

#: Số ID reserve khi UI tự gọi `architect.assign_ids`.
DEFAULT_ID_POOL = 3

KEY_ASSIGNED_IDS = "novel_ai_architect_assigned_ids"


def id_pool_key(artifact_type: str) -> str:
    """Khóa session state giữ pool ID vừa reserve cho một artifact."""
    return f"{KEY_ASSIGNED_IDS}_{artifact_type}"


# ---------------------------------------------------------------------------
# Helper thuần
# ---------------------------------------------------------------------------


def parse_assigned_ids(text: str, artifact_type: str) -> list[str]:
    """Parse danh sách ID user nhập; raise `GuardError` nếu sai prefix.

    Chấp nhận phân tách bằng dấu phẩy, khoảng trắng hoặc xuống dòng. Danh sách
    rỗng nghĩa là "để backend reserve pool mặc định".
    """
    raw = str(text or "").replace(",", " ").split()
    values: list[str] = []
    for item in raw:
        value = item.strip()
        if value and value not in values:
            values.append(value)
    expected = ASSIGNED_ID_PREFIXES.get(artifact_type)
    if expected is None and values:
        raise GuardError(
            f"`{artifact_type}` không dùng assigned ID entry (chỉ premise không cần).",
            code="unknown_artifact_type",
            details={"artifact_type": artifact_type},
        )
    for value in values:
        if expected is not None and not value.startswith(f"{expected}_"):
            raise GuardError(
                f"ID `{value}` không đúng prefix `{expected}_` của `{artifact_type}`; "
                "dùng `architect.assign_ids` để backend cấp ID.",
                code="invalid_assigned_id",
                details={"artifact_type": artifact_type, "assigned_id": value},
            )
    return values


def build_architect_action(
    *,
    artifact_type: str,
    action: str,
    chapter_number: int,
    assigned_ids: Sequence[str],
    user_instruction: str,
) -> dict[str, Any]:
    """Dựng tham số gọi `architect.generate` từ input UI (hàm thuần, test được).

    `assigned_ids` rỗng được truyền thành `None` để service tự reserve pool và
    trả về cảnh báo; như vậy UI không tự bịa ID.
    """
    return {
        "artifact_type": artifact_type,
        "action": action,
        "chapter_number": int(chapter_number),
        "assigned_ids": list(assigned_ids) or None,
        "user_instruction": str(user_instruction or "").strip(),
    }


def _payload_text(payload: Any) -> str:
    return _common.payload_json_text(payload)


def _revision_fingerprint(payload: Any) -> str:
    if payload is None:
        return "none"
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    digest = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def _entity_ids(payload: Any, artifact_type: str) -> list[str]:
    fields = _ENTITY_FIELDS.get(artifact_type)
    if fields is None:  # premise không có collection entry
        return []
    collection, id_field = fields
    return [
        str(getattr(item, id_field))
        for item in (getattr(payload, collection, None) or [])
    ]


def _accepted_entity_ids(project: Any, artifact_type: str) -> list[str]:
    envelope = _common.envelope_or_none(project, artifact_type)
    if envelope is None or envelope.accepted_revision is None:
        return []
    return _entity_ids(envelope.accepted_revision.payload, artifact_type)


def _appended_entity_ids(
    accepted_ids: Sequence[str], edited: Mapping[str, Any], artifact_type: str
) -> list[str]:
    """ID có trong payload append nhưng chưa có trong accepted (entry mới).

    Hàm chỉ **đọc** payload (không sửa) để nội dung user đang sửa không bị đổi
    ngoài ý muốn khi page hiển thị trước khi gọi service.
    """
    collection, id_field = _ENTITY_FIELDS[artifact_type]
    raw = edited.get(collection) or []
    result: list[str] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, Mapping):
            value = item.get(id_field)
            if value is not None and str(value) not in accepted_ids:
                result.append(str(value))
    return result


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Architect",
        "Guard nằm ở backend: Base Idea phải accepted, dependency không stale thì action mới chạy.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Architect.")
        return

    _render_auto_accept_note(project)

    artifact_type = st.radio(
        "Artifact",
        ARTIFACT_TYPES,
        format_func=lambda key: ARTIFACT_LABELS.get(key, key),
        horizontal=True,
        key="novel_ai_architect_artifact",
    )
    artifact_type = str(artifact_type)

    if ctx.registry is None and ctx.llm_client is None:
        st.error(
            "Chưa load được prompt registry v1 và chưa dựng được LLM client; "
            "action generate/regenerate không chạy được. Việc sửa tay và accept "
            "vẫn dùng được."
        )
    _render_state_panel(ctx, artifact_type)
    _render_generate_form(ctx, artifact_type)
    _render_candidate_panel(ctx, artifact_type)
    if artifact_type in APPEND_TYPES:
        _render_append_panel(ctx, artifact_type)


def _render_auto_accept_note(project: Any) -> None:
    import streamlit as st

    enabled = architect.auto_accept_enabled(project)
    label = "BẬT" if enabled else "TẮT"
    st.caption(
        f"Auto Accept structured: **{label}** (`project.config.auto_accept_structured`). "
        + _common.AUTO_ACCEPT_NOTE_STRUCTURED
    )
    st.caption(_common.AUTO_ACCEPT_NOTE_UNRELATED_TO_PROSE)


def _render_state_panel(ctx: AppContext, artifact_type: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, artifact_type)
    st.markdown(f"**Trạng thái `{artifact_type}`**")
    st.caption(_common.artifact_caption(envelope, label=artifact_type))
    if envelope is not None:
        if envelope.accepted_revision is not None:
            st.caption(
                _common.selected_revision_text(envelope, which="accepted")
                + " · payload source `"
                + envelope.accepted_revision.payload_source.source_type.value
                + "`"
            )
        if envelope.candidate_revision is not None:
            st.caption(_common.selected_revision_text(envelope, which="candidate"))
        for note in _common.artifact_stale_notes(envelope):
            st.warning(note)
        if envelope.status is ArtifactStatus.stale:
            st.info(
                "Artifact đang `stale`: vẫn accept lại được sau khi bạn review, miễn "
                "schema/ID/scope và pin còn hợp lệ. Stale không có nghĩa nội dung sai."
            )
    ids = _accepted_entity_ids(project, artifact_type)
    if ids:
        st.caption("ID đã có trong accepted: " + ", ".join(f"`{item}`" for item in ids))
    with st.expander("Xem payload accepted (canon hiện tại)"):
        if envelope is None or envelope.accepted_revision is None:
            st.caption("Chưa có accepted revision.")
        else:
            st.json(envelope.accepted_revision.payload.model_dump(mode="json"))


def _render_generate_form(ctx: AppContext, artifact_type: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    with st.form(f"novel_ai_architect_generate_{artifact_type}"):
        action = st.selectbox("Action", ACTIONS, key="novel_ai_architect_action")
        chapter_number = st.number_input(
            "effective_from_chapter cho entry mới (>= 1)",
            min_value=1,
            value=int(project.config.current_chapter),
            step=1,
            key="novel_ai_architect_chapter",
            help="Chỉ dùng cho Characters/World Rules/Foreshadow; Premise bỏ qua.",
        )
        instruction = st.text_area(
            "Yêu cầu thêm cho lần generate (tuỳ chọn)",
            key="novel_ai_architect_instruction",
            height=80,
        )
        assigned_text = ""
        assign_now = False
        if artifact_type in APPEND_TYPES:
            assigned_text = st.text_input(
                "assigned_ids (để trống = backend reserve pool mặc định)",
                value=", ".join(st.session_state.get(id_pool_key(artifact_type), [])),
                key=f"novel_ai_architect_assigned_text_{artifact_type}",
                help="ID phải do backend cấp; bấm nút bên dưới để reserve pool mới.",
            )
            assign_now = st.checkbox(
                "Reserve pool ID mới ngay bây giờ (backend `assign_ids`)",
                key=f"novel_ai_architect_assign_now_{artifact_type}",
            )
        attempt = st.number_input(
            _chapter_ui.ATTEMPT_LABEL,
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            key=f"novel_ai_architect_attempt_{artifact_type}",
        )
        submitted = st.form_submit_button("Chạy generate/regenerate/edit")

    pool_key = id_pool_key(artifact_type)
    if st.button("Reserve ID mới (backend `assign_ids`)", key=f"novel_ai_architect_assign_{artifact_type}"):
        try:
            pool = architect.assign_ids(project, artifact_type, DEFAULT_ID_POOL)
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step="Chọn artifact khác hoặc kiểm tra lại contract artifact_type.",
            )
        else:
            st.session_state[pool_key] = pool
            st.success(f"Đã reserve pool: {', '.join(pool)}")
    if not submitted:
        return

    if assign_now and artifact_type in APPEND_TYPES:
        try:
            pool = architect.assign_ids(project, artifact_type, DEFAULT_ID_POOL)
        except ServiceError as error:
            _common.render_service_error(
                error, next_step="Bỏ chọn reserve pool rồi chạy lại action."
            )
            return
        st.session_state[pool_key] = pool
        assigned_text = ", ".join(pool)
        st.caption(f"Pool vừa reserve: {', '.join(pool)}")

    try:
        assigned_ids = parse_assigned_ids(assigned_text, artifact_type)
    except GuardError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Sửa `assigned_ids` cho đúng prefix, hoặc để trống để backend tự "
                "reserve pool."
            ),
        )
        return

    if ctx.llm_client is None:
        st.error(
            "Chưa dựng được LLM client nên không gọi được `architect.generate`."
            + (f" {ctx.llm_error}" if ctx.llm_error else "")
        )
        return

    params = build_architect_action(
        artifact_type=artifact_type,
        action=str(action),
        chapter_number=int(chapter_number),
        assigned_ids=assigned_ids,
        user_instruction=instruction,
    )
    # `operation_id` tất định theo input ⇒ bấm lặp không gọi LLM lần hai và không
    # thay candidate đang xem (C3 của `docs/design/review-findings-t24.md`).
    operation_id = _chapter_ui.stable_operation_id(
        "architect",
        str(action),
        artifact_type,
        int(chapter_number),
        params["user_instruction"],
        int(attempt),
        ",".join(assigned_ids or []),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            f"novel_ai_architect_generate_{artifact_type}",
            intent=("generate", artifact_type, operation_id),
            project=project,
            call=lambda: architect.generate(
                project, client=ctx.llm_client, operation_id=operation_id, **params
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Đọc lỗi trên (field nào sai), sửa accepted dependency nếu là guard, "
                "rồi chạy lại. Accepted revision hiện tại không bị thay đổi."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _append_template(artifact_type: str) -> str:
    """Khung JSON rỗng cho entry append mới (chỉ collection, không kèm entry cũ)."""
    collection = _ENTITY_FIELDS[artifact_type][0]
    return json.dumps({collection: []}, ensure_ascii=False, indent=2)


def _candidate_editor(artifact_type: str, payload: Any, *, version: Any) -> str:
    """Text area giữ nội dung user đang sửa và nạp lại khi `version` đổi.

    Giá trị widget được quản lý qua `st.session_state[key]` (không truyền
    `value=` cho widget) để Streamlit không cảnh báo "default value + Session
    State" và để nội dung đang sửa không bị reset khi rerun.
    """
    import streamlit as st

    key = _common.editor_key(artifact_type, workspace="architect")
    text = _common.sync_text_editor(key, _payload_text(payload), version=version)
    return st.text_area(
        "Sửa candidate (JSON, validate theo field)",
        value=text,
        height=260,
        key=key,
    )


def _render_candidate_panel(ctx: AppContext, artifact_type: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, artifact_type)
    st.subheader("Candidate vs accepted")
    if envelope is None or envelope.candidate_revision is None:
        st.caption("Chưa có candidate nào đang chờ accept.")
        return
    candidate = envelope.candidate_revision
    accepted = envelope.accepted_revision
    st.caption(_common.candidate_update_hint_for_envelope(envelope))
    col_candidate, col_accepted = st.columns(2)
    with col_candidate:
        st.markdown(f"**Candidate r{candidate.revision}** (chưa accept)")
        st.caption(f"validation: `{candidate.validation.state.value}`")
        st.json(candidate.payload.model_dump(mode="json"))
    with col_accepted:
        st.markdown("**Accepted**")
        if accepted is None:
            st.caption("Chưa có accepted revision.")
        else:
            st.caption(f"r{accepted.revision} · accepted_by `{accepted.accepted_by or '—'}`")
            st.json(accepted.payload.model_dump(mode="json"))
    if candidate.validation.errors:
        st.warning("Candidate còn lỗi validation đã ghi nhận khi tạo:")
        for issue in candidate.validation.errors:
            st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")

    key = _common.editor_key(artifact_type, workspace="architect")
    version = (
        artifact_type,
        f"c{candidate.revision}:{_revision_fingerprint(candidate.payload)}",
        f"a{accepted.revision if accepted else 0}:{_revision_fingerprint(accepted.payload if accepted else None)}",
    )
    if st.button(
        "Nạp lại editor từ candidate", key=f"novel_ai_architect_reload_{artifact_type}"
    ):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    edited = _candidate_editor(artifact_type, candidate.payload, version=version)

    save_col, accept_col, reject_col = st.columns(3)
    with save_col:
        save_clicked = st.button(
            "Lưu candidate đã sửa",
            key=f"novel_ai_architect_edit_{artifact_type}",
        )
    with accept_col:
        accept_clicked = st.button(
            "Accept candidate", key=f"novel_ai_architect_accept_{artifact_type}"
        )
    with reject_col:
        reject_clicked = st.button(
            "Reject candidate", key=f"novel_ai_architect_reject_{artifact_type}"
        )

    if save_clicked:
        payload, parse_error = _common.parse_json_payload(edited)
        if parse_error:
            st.error(f"Chưa lưu được candidate: {parse_error}")
            st.info(
                "Bước tiếp theo: sửa JSON trong editor (nội dung bạn nhập vẫn còn) "
                "rồi bấm lưu lại."
            )
        else:
            try:
                result = architect.edit_candidate(
                    project, artifact_type=artifact_type, payload=payload
                )
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Sửa đúng field được nêu ở trên rồi lưu lại; accepted revision "
                        "giữ nguyên."
                    ),
                )
            else:
                set_action_result(result)
                _common.clear_session_keys(key, f"{key}__source")
                st.rerun()
    if accept_clicked:
        try:
            result = architect.accept(project, artifact_type=artifact_type)
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Nếu là stale/pin lệch: regenerate hoặc sửa candidate rồi accept lại. "
                    "Accepted cũ vẫn nguyên."
                ),
            )
        else:
            set_action_result(result)
            st.rerun()
    if reject_clicked:
        try:
            result = architect.reject(project, artifact_type=artifact_type)
        except ServiceError as error:
            _common.render_service_error(
                error, next_step="Candidate có thể đã bị reject trước đó; kiểm tra lại state."
            )
        else:
            set_action_result(result)
            st.rerun()


def _render_append_panel(ctx: AppContext, artifact_type: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    envelope = _common.envelope_or_none(project, artifact_type)
    st.subheader("Append entry mới (có `effective_from_chapter`)")
    st.caption(
        "Append chỉ nhận **entry mới** (ID chưa có trong accepted). Entry hiệu lực "
        "ở chương tương lai **không** làm chapter/plan quá khứ stale; entry hiệu lực "
        "<= `current_chapter` sẽ đánh dấu stale downstream liên quan (chỉ đánh dấu)."
    )
    if envelope is None or envelope.accepted_revision is None:
        st.info(
            "Chưa có accepted revision nên chưa append được. Bước tiếp theo: generate "
            "và accept artifact này trước."
        )
        return
    accepted = envelope.accepted_revision
    accepted_ids = _entity_ids(accepted.payload, artifact_type)
    effective_from_chapter = st.number_input(
        "`effective_from_chapter` cho entry mới (input rõ ràng, không suy từ payload)",
        min_value=1,
        value=int(project.config.current_chapter) + 1,
        step=1,
        key=f"novel_ai_architect_append_chapter_{artifact_type}",
    )
    st.caption(
        "Editor chỉ chứa **entry mới** (không chứa entry đã accepted): service tự ghép "
        "entry mới vào sau accepted và từ chối ID đã tồn tại. "
        f"ID đã có: {', '.join(f'`{item}`' for item in accepted_ids) or '(chưa có)'}."
    )
    key = _common.editor_key(artifact_type, workspace="architect", variant="append")
    version = (artifact_type, f"a{accepted.revision}:{_revision_fingerprint(accepted.payload)}")
    if st.button(
        "Nạp lại editor append từ accepted (khung entry mẫu)",
        key=f"novel_ai_architect_append_reload_{artifact_type}",
    ):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    baseline = _append_template(artifact_type)
    text = _common.sync_text_editor(key, baseline, version=version)
    edited = st.text_area(
        "Entry mới cần append (JSON, cùng shape payload accepted)",
        value=text,
        height=240,
        key=key,
    )
    if st.button("Append entry", key=f"novel_ai_architect_append_{artifact_type}"):
        payload, parse_error = _common.parse_json_payload(edited)
        if parse_error:
            st.error(f"Chưa append được: {parse_error}")
            st.info(
                "Bước tiếp theo: thêm entry mới vào JSON trong editor (ID mới, chưa "
                "trùng accepted) rồi bấm lại."
            )
            return
        appended_ids = _appended_entity_ids(accepted_ids, payload, artifact_type)
        st.caption(
            "Entry mới phát hiện được: "
            + (", ".join(f"`{item}`" for item in appended_ids) if appended_ids else "(chưa có)")
            + f" · sẽ nhận `effective_from_chapter={int(effective_from_chapter)}`."
        )
        try:
            result = architect.append_entries(
                project,
                artifact_type=artifact_type,
                payload=payload,
                effective_from_chapter=int(effective_from_chapter),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Thêm entry mới với stable ID chưa dùng, giữ nguyên các entry cũ, "
                    "rồi append lại; accepted giữ nguyên nếu lỗi."
                ),
            )
        else:
            set_action_result(result)
            _common.clear_session_keys(key, f"{key}__source")
            st.rerun()

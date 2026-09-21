"""Workspace Revision: revise upstream, retcon, stale chain và recovery (T22).

Page này hiện thực `docs/design/workflow.md` mục 4.1, 4.5, 5.2, 6.4 và
`docs/design/storage.md` mục 8, 10, 11:

- **Revise Base Idea / Revise Premise**: hiển thị phạm vi và hệ quả stale **trước**
  khi bấm; mọi thay đổi đều tạo accepted revision mới + đánh dấu stale downstream,
  **không** rewrite Final Manuscript;
- **Start Retcon** chỉ cho chapter đã `final_reconciled`; hiển thị `retcon_state` và
  nói rõ **final cũ vẫn là canon** cho tới khi commit (D004);
- **Impact report** chỉ trình bày: report được lưu thành candidate, không tự apply
  và không tự đánh dấu stale;
- **Review/Reaccept stale artifact** qua `revision.reaccept_stale` (nếu vẫn không
  hợp lệ thì artifact giữ `stale`);
- **Downstream blockers**: dẫn người dùng `reset_consistency_after_retcon` rồi
  `reconcile_downstream` **lần lượt** theo thứ tự chương;
- **Recovery**: hiển thị pending transaction và retry an toàn qua
  `reconcile.recover`; khi `storage.requires_manual_recovery` project là read-only
  nên UI **không** render action ghi nào;
- **History/snapshot**: đối chiếu accepted cũ, candidate mới và `finalizing`.

Không có Git UI, không restore tùy ý: chỉ đọc để người dùng đối chiếu.
"""

from __future__ import annotations

from typing import Any

from novel_ai.core import storage
from novel_ai.services import ServiceError, reconcile
from novel_ai.services import revision as revision_service
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "IMPACT_KINDS",
    "KEY_BASE_IDEA_TEXT",
    "KEY_BLOCKER_DOWNSTREAM_PREFIX",
    "KEY_BLOCKER_REACCEPT_PREFIX",
    "KEY_IMPACT_AFTER",
    "KEY_IMPACT_ATTEMPT",
    "KEY_IMPACT_BEFORE",
    "KEY_IMPACT_ID",
    "KEY_IMPACT_KIND",
    "KEY_IMPACT_SCOPE",
    "KEY_IMPACT_FROM",
    "KEY_IMPACT_RUN",
    "KEY_IMPACT_TO",
    "KEY_PREMISE_EDITOR",
    "KEY_RECOVER",
    "KEY_RESET_CONSISTENCY",
    "KEY_RETCON_CHAPTER",
    "KEY_REVISE_BASE",
    "KEY_REVISE_PREMISE",
    "KEY_START_RETCON",
    "RENDER_SECTIONS",
    "render",
]

#: `item_kind` hợp lệ cho `SourceChange` của impact report.
IMPACT_KINDS: tuple[str, ...] = (
    "chapter",
    "premise",
    "characters",
    "world_rules",
    "foreshadow",
    "long_plan",
    "short_plan",
    "skeleton",
)

#: Thứ tự panel mutation (dùng cho test/manual checklist).
RENDER_SECTIONS: tuple[str, ...] = (
    "recovery",
    "blockers",
    "retcon",
    "revise",
    "impact",
    "history",
)

KEY_RECOVER = "novel_ai_revision_recover"
KEY_BLOCKER_REACCEPT_PREFIX = "novel_ai_revision_reaccept_"
KEY_BLOCKER_DOWNSTREAM_PREFIX = "novel_ai_revision_downstream_"
KEY_RETCON_CHAPTER = "novel_ai_revision_retcon_chapter"
KEY_START_RETCON = "novel_ai_revision_start_retcon"
KEY_RESET_CONSISTENCY = "novel_ai_revision_reset_consistency"
KEY_BASE_IDEA_TEXT = "novel_ai_revision_base_idea_text"
KEY_REVISE_BASE = "novel_ai_revision_revise_base"
KEY_PREMISE_EDITOR = _common.editor_key("premise", workspace="revision")
KEY_REVISE_PREMISE = "novel_ai_revision_revise_premise"
KEY_IMPACT_KIND = "novel_ai_revision_impact_kind"
KEY_IMPACT_ID = "novel_ai_revision_impact_id"
KEY_IMPACT_FROM = "novel_ai_revision_impact_from"
KEY_IMPACT_TO = "novel_ai_revision_impact_to"
KEY_IMPACT_BEFORE = "novel_ai_revision_impact_before"
KEY_IMPACT_AFTER = "novel_ai_revision_impact_after"
KEY_IMPACT_SCOPE = "novel_ai_revision_impact_scope"
KEY_IMPACT_ATTEMPT = "novel_ai_revision_impact_attempt"
KEY_IMPACT_RUN = "novel_ai_revision_impact_run"


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Revision",
        "Sửa upstream/retcon là action rõ ràng: backend chỉ đánh dấu stale, không tự rewrite downstream.",
    )
    show_action_result()
    _chapter_ui.show_note("revision")

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Revision.")
        return

    recovery = _chapter_ui.recovery_state(project)
    _render_recovery_panel(ctx, recovery)
    if recovery.manual:
        st.error(
            "Project đang **read-only**: UI không render action ghi nào (revise/retcon/"
            "reaccept/reconcile) cho tới khi bạn xử lý thủ công `.ops/pending/` của project. "
            "Mục History bên dưới vẫn đọc được để đối chiếu."
        )
        _render_history_panel(project)
        return

    _render_blockers_panel(ctx)
    _render_retcon_panel(ctx)
    _render_revise_panel(ctx)
    _render_impact_panel(ctx)
    _render_history_panel(project)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _render_recovery_panel(ctx: AppContext, recovery: _chapter_ui.RecoveryState) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Recovery")
    if recovery.manual:
        st.error(recovery.summary())
        st.caption(
            "Bước tiếp theo: kiểm tra `.ops/pending/` và `history/` của project, khôi phục "
            "file bị sửa ngoài app. UI **không** tự ghi đè dữ liệu."
        )
        return
    if not recovery.needs_recovery:
        st.caption(recovery.summary())
        return
    st.warning(recovery.summary())
    st.caption(
        "Backend chỉ hoàn tất commit đã staged và không merge trùng; retry cùng "
        "`operation_id` trả kết quả cũ."
    )
    if st.button("Chạy recovery (reconcile.recover)", key=KEY_RECOVER):
        intent = (tuple(recovery.pending), "recover")
        try:
            blocked, report = _chapter_ui.run_action(
                KEY_RECOVER,
                intent=intent,
                project=project,
                call=lambda: reconcile.recover(project),
            )
        except ServiceError as error:  # pragma: no cover - recover_pending không raise
            _common.render_service_error(
                error, next_step="Đọc `.ops/pending/` của project rồi thử lại."
            )
            return
        if blocked:
            return
        for message in report.messages:
            st.info(message)
        if report.status == storage.RECOVERY_MANUAL:
            st.error(
                "Recovery cần người xử lý (target bị sửa ngoài app hoặc thiếu staged file). "
                "Xem `.ops/pending/` và `history/`."
            )
            return
        _chapter_ui.set_note(
            "revision",
            f"Recovery hoàn tất (`{report.status}`) · operation {list(report.operation_ids)} · "
            f"applied {list(report.applied_paths)}",
        )
        st.rerun()


# ---------------------------------------------------------------------------
# Stale chain
# ---------------------------------------------------------------------------


def _render_blockers_panel(ctx: AppContext) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Stale chain và downstream blockers")
    blockers = _chapter_ui.stale_blockers(project)
    if not blockers:
        st.caption(
            "Không có artifact/state nào đang stale. Writer không bị chặn bởi stale chain."
        )
        return

    artifacts = [item for item in blockers if item.get("kind") == "artifact"]
    chapters = [
        item
        for item in blockers
        if item.get("kind") in {"timeline_entry", "relationship"}
    ]
    finalizing = [item for item in blockers if item.get("kind") == "chapter"]

    if artifacts:
        st.markdown("**Artifact stale** (review/reaccept hoặc regenerate):")
        for item in artifacts:
            artifact_id = str(item.get("artifact_id"))
            st.markdown(
                f"- `{artifact_id}` (`{item.get('artifact_type')}`) — {item.get('reason')}"
            )
            if st.button(
                f"Review/Reaccept `{artifact_id}`",
                key=f"{KEY_BLOCKER_REACCEPT_PREFIX}{artifact_id}",
            ):
                _run_reaccept(project, artifact_id)

    if chapters:
        st.markdown(
            "**State chain stale sau retcon** — rebuild **lần lượt** theo thứ tự chương:"
        )
        rows = _chapter_ui.chapter_rows(project)
        by_number = {int(row["chapter_number"]): str(row["chapter_id"]) for row in rows}
        targets: dict[int, dict[str, Any]] = {}
        for item in chapters:
            number = item.get("chapter_number")
            if not isinstance(number, int):
                continue
            targets.setdefault(number, item)
        ordered = sorted(targets)
        next_number = ordered[0]
        for number in ordered:
            item = targets[number]
            chapter_id = str(item.get("chapter_id") or by_number.get(number) or "")
            label = f"Reconcile downstream chương {number} (`{chapter_id}`)"
            if number == next_number:
                st.caption(f"Bước kế tiếp: {label} — dùng state đã rebuild của chương {number - 1}.")
                if st.button(label, key=f"{KEY_BLOCKER_DOWNSTREAM_PREFIX}{chapter_id or number}"):
                    _run_reconcile_downstream(ctx, chapter_id)
            else:
                st.button(
                    label,
                    key=f"{KEY_BLOCKER_DOWNSTREAM_PREFIX}{chapter_id or number}",
                    disabled=True,
                    help="Rebuild theo thứ tự: hoàn tất chương trước rồi mới tới chương này.",
                )
        st.caption(
            "Nếu chain lệch vì retcon, chạy `Reset consistency sau retcon` ở panel Retcon "
            "trước khi rebuild."
        )

    if finalizing:
        st.markdown("**Chapter đang `finalizing`** (chưa phải canon):")
        for item in finalizing:
            st.markdown(
                f"- `{item.get('chapter_id')}` — {item.get('reason')} → workspace **Reconcile**."
            )


def _run_reaccept(project: Any, artifact_id: str) -> None:
    import streamlit as st

    envelope = _common.envelope_or_none(project, artifact_id)
    revision = (
        envelope.accepted_revision.revision
        if envelope is not None and envelope.accepted_revision is not None
        else None
    )
    intent = (artifact_id, revision)
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_BLOCKER_REACCEPT_PREFIX + artifact_id,
            intent=intent,
            project=project,
            call=lambda: revision_service.reaccept_stale(
                project,
                artifact_id=artifact_id,
                operation_id=_chapter_ui.stable_operation_id("revision", "reaccept", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nếu artifact vẫn dựa trên bản upstream cũ thì reaccept bị từ chối: hãy "
                "regenerate artifact đó, hoặc revise tiếp cho tới khi pin khớp."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


def _run_reconcile_downstream(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    if not chapter_id:
        st.error("Blocker không gắn `chapter_id`; mở workspace Reconcile để xử lý thủ công.")
        return
    if ctx.llm_client is None or ctx.registry is None:
        st.error("Chưa dùng được reconcile downstream: thiếu prompt registry hoặc LLM client.")
        return
    attempt = _chapter_ui.attempt_input(f"novel_ai_revision_downstream_attempt_{chapter_id}")
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    intent = (
        chapter_id,
        int(attempt),
        chapter.final_revision.revision if chapter is not None and chapter.final_revision else None,
        _chapter_ui.downstream_state_version(project),
    )
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_BLOCKER_DOWNSTREAM_PREFIX + chapter_id,
            intent=intent,
            project=project,
            call=lambda: revision_service.reconcile_downstream(
                project,
                client=ctx.llm_client,
                chapter_id=chapter_id,
                operation_id=_chapter_ui.stable_operation_id(
                    "revision", "reconcile_downstream", *intent
                ),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "State giữ nguyên và Writer vẫn bị khóa. Kiểm tra proposal/retcon rồi chạy "
                "lại theo thứ tự chương."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    st.rerun()


# ---------------------------------------------------------------------------
# Retcon
# ---------------------------------------------------------------------------


def _render_retcon_panel(ctx: AppContext) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Retcon chapter đã final")
    final_rows = _chapter_ui.final_chapters(project)
    if not final_rows:
        st.info(
            "Chưa có chapter `final_reconciled` nào. Retcon chỉ bắt đầu từ Final Manuscript."
        )
        return
    chapter_id = _chapter_ui.chapter_select(
        KEY_RETCON_CHAPTER,
        final_rows,
        label="Chapter đã final",
    )
    if chapter_id is None:  # pragma: no cover
        return

    st.caption("Phạm vi và hệ quả khi retcon (không rewrite final prose của chương sau):")
    for line in _chapter_ui.upstream_stale_scope("chapter_retcon"):
        st.markdown(f"- {line}")

    marker = _chapter_ui.retcon_marker(project, chapter_id)
    if marker is None:
        st.caption(
            "`Start Retcon` tạo draft retcon copy từ Final Manuscript. Final cũ **vẫn là "
            "canon** cho tới khi retcon commit (D004)."
        )
        if st.button("Start Retcon", key=KEY_START_RETCON):
            intent = (chapter_id, "start_retcon")
            try:
                blocked, result = _chapter_ui.run_action(
                    KEY_START_RETCON,
                    intent=intent,
                    project=project,
                    call=lambda: revision_service.start_retcon(
                        project,
                        chapter_id=chapter_id,
                        operation_id=_chapter_ui.stable_operation_id(
                            "revision", "start_retcon", *intent
                        ),
                    ),
                )
            except ServiceError as error:
                _common.render_service_error(
                    error,
                    next_step=(
                        "Retcon chỉ chạy cho chapter `final_reconciled` và khi chưa có retcon "
                        "nào đang mở."
                    ),
                )
                return
            if blocked:
                return
            set_action_result(result)
            st.rerun()
        return

    st.warning(
        f"Đang retcon: draft r{marker.get('retcon_draft_revision')} · "
        f"thay `r{marker.get('replaces_final_revision')}` · "
        f"final cũ vẫn canon `{marker.get('final_still_canon')}` · mở lúc "
        f"{marker.get('started_at')}"
    )
    st.caption(
        "Luồng hoàn tất: sửa/review draft retcon (draft đang là `current_draft_revision`), "
        "rồi Finalize ở workspace **Review** (action sẽ xác nhận Human Review), rồi "
        "Accept reconciliation ở workspace **Reconcile**."
    )
    st.info(
        "Giới hạn MVP đã biết: `write`/`reviewer` guard `chapter_already_final` chặn Save "
        "Draft/AI Review/Apply rewrite trên chapter đang `final_reconciled`, nên nội dung "
        "draft retcon hiện chỉ đổi được bằng action retcon/upstream revise, không sửa tay "
        "qua workspace Writer/Review."
    )
    if st.button(
        "Reset consistency sau retcon (đánh dấu stale downstream)",
        key=KEY_RESET_CONSISTENCY,
    ):
        chapter = _chapter_ui.chapter_by_id(project, chapter_id)
        number = chapter.chapter_number if chapter is not None else 0
        intent = (chapter_id, number, _chapter_ui.downstream_state_version(project))
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_RESET_CONSISTENCY,
                intent=intent,
                project=project,
                call=lambda: revision_service.reset_consistency_after_retcon(
                    project,
                    chapter_number=number,
                    operation_id=_chapter_ui.stable_operation_id(
                        "revision", "reset_consistency", *intent
                    ),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step="Kiểm tra chapter number rồi chạy lại; state accepted không bị ghi lùi.",
            )
            return
        if blocked:
            return
        set_action_result(result)
        st.rerun()


# ---------------------------------------------------------------------------
# Revise Base Idea / Premise
# ---------------------------------------------------------------------------


def _render_revise_panel(ctx: AppContext) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Revise upstream (Base Idea / Premise)")
    _render_revise_base_idea(project)
    _render_revise_premise(project)


def _render_revise_base_idea(project: Any) -> None:
    import streamlit as st

    st.markdown("**Revise Base Idea**")
    meta = _chapter_ui.read_json_relpath(project, "idea/base_idea.meta.json")
    current_text = _chapter_ui.read_text_relpath(project, "idea/base_idea.md")
    if isinstance(meta, dict):
        st.caption(
            f"Accepted hiện tại: r{meta.get('revision')} · status `{meta.get('status')}` · "
            f"accepted_by `{meta.get('accepted_by')}`"
        )
    else:
        st.caption("Chưa đọc được metadata Base Idea.")
    for line in _chapter_ui.upstream_stale_scope("base_idea"):
        st.markdown(f"- Hệ quả: {line}")
    text = st.text_area(
        "Base Idea (markdown) — bản mới sẽ là accepted revision mới",
        value=current_text,
        height=200,
        key=KEY_BASE_IDEA_TEXT,
    )
    if not st.button("Revise Base Idea", key=KEY_REVISE_BASE):
        return
    if text.strip() and text.strip() == current_text.strip():
        st.info(
            "Nội dung gửi lên **trùng** Base Idea accepted hiện tại, nên backend sẽ không tạo "
            "revision mới (chống bấm lặp). Sửa nội dung rồi bấm lại nếu bạn thật sự muốn revise."
        )
        return
    intent = ("base_idea", _chapter_ui.payload_fingerprint(text))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_REVISE_BASE,
            intent=intent,
            project=project,
            call=lambda: revision_service.revise_base_idea(
                project,
                markdown=text,
                operation_id=_chapter_ui.stable_operation_id("revision", "revise_base_idea", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Base Idea rỗng bị từ chối và bản accepted cũ được giữ; sửa nội dung rồi "
                "bấm lại."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(KEY_BASE_IDEA_TEXT, f"{KEY_BASE_IDEA_TEXT}__source")
    st.rerun()


def _render_revise_premise(project: Any) -> None:
    import streamlit as st

    st.markdown("**Revise Premise**")
    envelope = _common.envelope_or_none(project, "premise")
    if envelope is None or envelope.accepted_revision is None:
        st.info("Chưa có Premise accepted để revise. Hoàn tất Architect trước.")
        return
    accepted = envelope.accepted_revision
    st.caption(
        f"Accepted hiện tại: r{accepted.revision} · "
        f"`{_chapter_ui.payload_fingerprint(accepted.payload)}` (fingerprint payload)"
    )
    for line in _chapter_ui.upstream_stale_scope("premise"):
        st.markdown(f"- Hệ quả: {line}")
    source_text = _common.payload_json_text(accepted.payload)
    version = (accepted.revision, _chapter_ui.payload_fingerprint(accepted.payload))
    text = _common.sync_text_editor(KEY_PREMISE_EDITOR, source_text, version=version)
    edited = st.text_area(
        "Premise (JSON) — sửa rồi gửi để tạo accepted revision mới + stale downstream",
        value=text,
        height=260,
        key=KEY_PREMISE_EDITOR,
    )
    if not st.button("Revise Premise", key=KEY_REVISE_PREMISE):
        return
    payload, parse_error = _common.parse_json_payload(edited)
    if parse_error:
        st.error(f"Chưa gửi được Premise revision: {parse_error}")
        st.info("Sửa JSON trong editor (nội dung bạn nhập vẫn còn) rồi bấm lại.")
        return
    if _chapter_ui.payload_fingerprint(payload) == _chapter_ui.payload_fingerprint(
        accepted.payload
    ):
        st.info(
            "Payload gửi lên **trùng** Premise accepted hiện tại, nên backend sẽ không tạo "
            "revision mới (chống bấm lặp). Sửa nội dung rồi bấm lại nếu cần."
        )
        return
    intent = ("premise", _chapter_ui.payload_fingerprint(payload))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_REVISE_PREMISE,
            intent=intent,
            project=project,
            call=lambda: revision_service.revise_premise(
                project,
                payload=payload,
                operation_id=_chapter_ui.stable_operation_id("revision", "revise_premise", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Sửa đúng field được nêu ở trên; Base Idea và Final Manuscript không đổi, "
                "accepted Premise cũ vẫn giữ nếu lỗi."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(KEY_PREMISE_EDITOR, f"{KEY_PREMISE_EDITOR}__source")
    st.rerun()


# ---------------------------------------------------------------------------
# Impact report
# ---------------------------------------------------------------------------


def _render_impact_panel(ctx: AppContext) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Impact report (chỉ trình bày)")
    st.caption(
        "Report là candidate `draft`: **không** tự apply, **không** tự đánh dấu stale. Việc "
        "đánh dấu stale thuộc backend deterministic (`mark_downstream_stale`)."
    )
    kind = st.selectbox("`item_kind` của thay đổi", IMPACT_KINDS, key=KEY_IMPACT_KIND)
    item_id = st.text_input(
        "`item_id` (ví dụ `ch_0001`, `premise`, `characters`)", key=KEY_IMPACT_ID
    )
    col_from, col_to = st.columns(2)
    with col_from:
        from_revision = st.number_input(
            "`from_revision` (0 = không khai)", min_value=0, value=0, step=1, key=KEY_IMPACT_FROM
        )
    with col_to:
        to_revision = st.number_input(
            "`to_candidate_revision` (0 = không khai)",
            min_value=0,
            value=0,
            step=1,
            key=KEY_IMPACT_TO,
        )
    before = st.text_area("Nội dung trước thay đổi (tuỳ chọn)", key=KEY_IMPACT_BEFORE, height=100)
    after = st.text_area("Nội dung sau thay đổi (tuỳ chọn)", key=KEY_IMPACT_AFTER, height=100)
    scope = st.text_input("`analysis_scope` (tuỳ chọn)", key=KEY_IMPACT_SCOPE)
    attempt = _chapter_ui.attempt_input(KEY_IMPACT_ATTEMPT)

    if ctx.llm_client is None or ctx.registry is None:
        st.error("Chưa dùng được impact report: thiếu prompt registry hoặc LLM client.")
    elif st.button("Chạy impact report", key=KEY_IMPACT_RUN):
        source_change = {
            "item_kind": str(kind),
            "item_id": str(item_id or "").strip(),
            "from_revision": int(from_revision) or None,
            "to_candidate_revision": int(to_revision) or None,
        }
        intent = (
            source_change,
            str(before or ""),
            str(after or ""),
            str(scope or ""),
            int(attempt),
        )
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_IMPACT_RUN,
                intent=intent,
                project=project,
                call=lambda: revision_service.generate_impact_report(
                    project,
                    client=ctx.llm_client,
                    source_change=source_change,
                    before_content=str(before or ""),
                    after_content=str(after or ""),
                    analysis_scope=str(scope or ""),
                    operation_id=_chapter_ui.stable_operation_id(
                        "revision", "impact_report", source_change["item_id"], *intent
                    ),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Điền `item_id` hợp lệ rồi chạy lại; không mutation nào được thực hiện khi "
                    "report lỗi."
                ),
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()

    _render_stored_impact(project, str(item_id or "").strip() or None)


def _render_stored_impact(project: Any, item_id: str | None) -> None:
    """Hiển thị report đã lưu (nếu có) để đọc lại sau reload."""
    import streamlit as st

    if not item_id:
        return
    artifact_id = f"impact_report_{item_id}"
    envelope = _common.envelope_or_none(project, artifact_id)
    if envelope is None:
        return
    revision = envelope.candidate_revision or envelope.accepted_revision
    if revision is None:
        return
    payload = revision.payload
    items = list(getattr(payload, "affected_items", []) or [])
    st.markdown(
        f"**Report r{revision.revision}** (`{envelope.status.value}`) — "
        f"{getattr(payload, 'risk_summary', '')}"
    )
    if not items:
        st.caption("Report không nêu item nào bị ảnh hưởng.")
    for item in items:
        st.markdown(
            f"- `{item.item_id}` (`{item.item_kind}`) · [{item.severity.value}] {item.reason}"
            + (f" → {item.suggested_action}" if item.suggested_action else "")
        )
    for action in list(getattr(payload, "suggested_actions", []) or []):
        st.caption(f"Gợi ý: {action}")
    st.caption("Report không đổi accepted state; stale marking vẫn phải do bạn quyết định.")


# ---------------------------------------------------------------------------
# History / snapshot
# ---------------------------------------------------------------------------


def _render_history_panel(project: Any) -> None:
    import streamlit as st

    st.subheader("History và snapshot (đối chiếu phiên bản)")
    rows = _chapter_ui.version_rows(project)
    if rows:
        st.markdown("**Chapter: accepted cũ / candidate mới / `finalizing`**")
        for row in rows:
            st.markdown(
                f"- Chương {row.chapter_number} · `{row.chapter_id}` — {row.note()}"
            )
    artifacts = _chapter_ui.artifact_rows(project)
    if artifacts:
        with st.expander("Artifact envelope (accepted/candidate/stale)"):
            for item in artifacts:
                st.markdown(
                    f"- `{item['artifact_id']}` (`{item['artifact_type']}`) · "
                    f"status `{item['status']}` · accepted r{item['accepted_revision']} · "
                    f"candidate r{item['candidate_revision']}"
                )
                for reason in item["stale_reasons"]:
                    st.caption(f"stale: {reason}")
    snapshots = _chapter_ui.snapshot_rows(project)
    with st.expander(f"Snapshot context ({len(snapshots)})"):
        if not snapshots:
            st.caption("Chưa có snapshot nào.")
        for item in snapshots:
            st.markdown(
                f"- `{item['snapshot_id']}` · chương {item['for_chapter_number']} "
                f"(`{item['for_chapter_id']}`) · action `{item['created_from_action']}` · "
                f"{item['dependency_pins']} pin · {item['created_at']}"
            )
    history = _chapter_ui.history_entries(project)
    with st.expander(f"History operation ({len(history)})"):
        if not history:
            st.caption("Chưa có bản history nào (chưa thay file đã tồn tại).")
        for item in history[-20:]:
            st.markdown(
                f"- `{item['operation_id']}` · `{item['operation_type']}` · "
                f"status `{item['status']}` · {item['created_at']} · "
                f"{len(item['applied_paths'])} applied · {len(item['before_files'])} before"
            )
    st.caption(
        "UI chỉ hiển thị để đối chiếu; không có Git UI và không restore tùy ý. Restore/undo "
        "phải đi qua action service tương ứng (retcon/reconcile/regenerate)."
    )

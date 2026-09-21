"""Workspace Reconcile: hàng đợi finalize, proposal JSON, accept/reject/cancel (T21).

Page này hiện thực `docs/design/workflow.md` mục 3.2, 4.4, 5.5 và D003:

- hiển thị **hàng đợi** chapter đang `finalizing` cùng final candidate và trạng thái
  reconciliation (`missing`/`draft`/`failed`/`accepted`);
- proposal JSON sửa được (`reconcile.edit_reconciliation_candidate`), Accept/Reject/
  Retry, và `reconcile.pending_reconciliation` là nguồn hiển thị;
- **Accept** commit transaction nhiều file; chỉ sau đó chapter mới
  `final_reconciled` và chương sau mới mở. Trước đó UI luôn nói chương sau **vẫn khóa**;
- có nút **Cancel finalizing** (bỏ final candidate, về `review_required`, accepted
  state không đổi);
- reload giữa `finalizing` vẫn đúng bước vì mọi thứ đọc từ file project;
- lỗi JSON/schema chỉ hiện lỗi theo field, accepted state không đổi.
"""

from __future__ import annotations

from typing import Any

from novel_ai.core import storage
from novel_ai.services import ServiceError, reconcile
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _chapter_ui, _common

__all__ = [
    "KEY_ACCEPT",
    "KEY_ATTEMPT",
    "KEY_CANCEL",
    "KEY_CHAPTER",
    "KEY_EDIT_JSON",
    "KEY_GENERATE",
    "KEY_REJECT",
    "KEY_RELOAD_JSON",
    "KEY_RETRY",
    "render",
]

KEY_CHAPTER = "novel_ai_reconcile_chapter"
KEY_ATTEMPT = "novel_ai_reconcile_attempt"
KEY_GENERATE = "novel_ai_reconcile_generate"
KEY_RETRY = "novel_ai_reconcile_retry"
KEY_RELOAD_JSON = "novel_ai_reconcile_reload_json"
KEY_EDIT_JSON = "novel_ai_reconcile_edit_json"
KEY_ACCEPT = "novel_ai_reconcile_accept"
KEY_REJECT = "novel_ai_reconcile_reject"
KEY_CANCEL = "novel_ai_reconcile_cancel"


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Reconcile",
        "Finalize chỉ đóng băng final candidate; chapter thành canon sau khi reconciliation commit.",
    )
    show_action_result()
    _chapter_ui.show_note("reconcile")

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Reconcile.")
        return

    recovery = _chapter_ui.recovery_state(project)
    if recovery.needs_recovery:
        st.warning(
            recovery.summary()
            + " Bước tiếp theo: workspace **Revision** → mục Recovery để chạy "
            "`reconcile.recover` trước khi commit tiếp."
        )

    rows = _chapter_ui.chapter_rows(project)
    if not rows:
        st.info("Chưa có chapter nào. Hoàn tất Short Plan accepted trước.")
        return

    queue = _chapter_ui.reconcile_queue(project)
    _render_queue_panel(project, queue, rows)
    finalizing = [item.chapter_id for item in queue]
    if not finalizing:
        st.info(
            "Không có chapter nào đang `finalizing`. Bước tiếp theo: workspace **Review** → "
            "`Finalize Chapter` cho chapter đã được Human Review."
        )
        return

    chapter_id = _chapter_ui.chapter_select(
        KEY_CHAPTER, rows, label="Chapter đang finalizing", statuses=["finalizing"]
    )
    if chapter_id is None:  # pragma: no cover - có finalizing nên luôn có lựa chọn
        return

    _render_pending_panel(project, chapter_id)
    _render_proposal_panel(ctx, chapter_id)
    _render_actions_panel(ctx, chapter_id)


def _render_queue_panel(project: Any, queue: list[Any], rows: list[dict[str, Any]]) -> None:
    import streamlit as st

    st.subheader("Hàng đợi finalize/reconcile")
    if queue:
        for item in queue:
            st.markdown(f"- {item.label()}")
    else:
        st.caption("Hàng đợi trống.")
    done = [row for row in rows if row["status"] == "final_reconciled"]
    if done:
        st.caption(
            "Đã `final_reconciled`: "
            + " · ".join(
                f"chương {row['chapter_number']} (`{row['chapter_id']}`) final r{row['final_revision']}"
                for row in done
            )
        )


def _render_pending_panel(project: Any, chapter_id: str) -> None:
    """Trạng thái finalizing/reconciliation đọc từ `reconcile.pending_reconciliation`."""
    import streamlit as st

    info = _chapter_ui.pending_view(project, chapter_id)
    st.subheader(f"Trạng thái `{chapter_id}`")
    if info is None:
        st.warning("Không đọc được trạng thái chapter.")
        return
    candidate = info.get("final_candidate") or {}
    st.markdown(
        f"- chapter status: `{info.get('status')}`\n"
        f"- final candidate: prose r{candidate.get('prose_revision')} · "
        f"`{candidate.get('markdown_ref')}` · reconciliation "
        f"`{candidate.get('reconciliation_status')}`\n"
        f"- reconciliation artifact `{info.get('reconciliation_artifact_id')}`: "
        f"`{info.get('reconciliation_status')}` · candidate r{info.get('candidate_revision')} · "
        f"accepted r{info.get('accepted_revision')}\n"
        f"- recovery: needs_recovery `{info.get('needs_recovery')}` · "
        f"manual `{info.get('requires_manual_recovery')}` · "
        f"pending {list(info.get('pending_operation_ids') or [])}"
    )
    st.warning(
        "Chapter đang `finalizing`: final candidate **chưa** là canon và chương sau "
        "**vẫn khóa**. Chỉ accept reconciliation thành công mới mở chương sau (D003)."
    )
    markdown_ref = candidate.get("markdown_ref")
    if markdown_ref:
        text = _chapter_ui.read_text_relpath(project, str(markdown_ref))
        with st.expander("Xem final candidate (bản prose đang chờ commit)"):
            st.code(text or "(không đọc được file final candidate)", language="text")


def _render_proposal_panel(ctx: AppContext, chapter_id: str) -> None:
    """JSON proposal: sửa tay rồi validate lại qua service (accepted không đổi)."""
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Reconciliation proposal")
    info = _chapter_ui.pending_view(project, chapter_id) or {}
    artifact_id = str(info.get("reconciliation_artifact_id") or f"reconciliation_{chapter_id}")
    envelope = _common.envelope_or_none(project, artifact_id)

    if envelope is None or envelope.candidate_revision is None:
        if envelope is not None and envelope.accepted_revision is not None:
            st.caption(
                "Artifact đang `"
                + envelope.status.value
                + "` với accepted r"
                + str(envelope.accepted_revision.revision)
                + " (proposal cũ vẫn là pin hợp lệ cho tới khi commit)."
            )
        st.info(
            "Chưa có proposal candidate. Bước tiếp theo: bấm `Generate reconciliation` "
            "(gọi LLM) hoặc `Retry reconcile`."
        )
        return

    candidate = envelope.candidate_revision
    st.caption(
        f"Candidate r{candidate.revision} · validation `{candidate.validation.state.value}` · "
        f"nguồn `{candidate.payload_source.source_type.value}`"
    )
    if candidate.validation.errors:
        st.warning("Proposal candidate còn lỗi validation đã ghi nhận:")
        for issue in candidate.validation.errors:
            st.markdown(f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_")

    key = _common.editor_key("reconciliation", workspace="reconcile")
    version = (
        chapter_id,
        f"c{candidate.revision}:{_chapter_ui.payload_fingerprint(candidate.payload)}",
    )
    if st.button("Nạp lại JSON từ candidate", key=KEY_RELOAD_JSON):
        _common.clear_session_keys(key, f"{key}__source")
        st.rerun()
    text = _common.sync_text_editor(
        key, _common.payload_json_text(candidate.payload), version=version
    )
    edited = st.text_area(
        "Proposal JSON (sửa tay được; backend validate lại theo schema/ID/freshness)",
        value=text,
        height=320,
        key=key,
    )
    st.caption(
        "Sửa tay **không** commit gì: proposal vẫn là candidate cho tới khi bạn bấm Accept."
    )
    if not st.button("Lưu JSON đã sửa (validate lại)", key=KEY_EDIT_JSON):
        return
    payload, parse_error = _common.parse_json_payload(edited)
    if parse_error:
        st.error(f"Chưa lưu được proposal: {parse_error}")
        st.info(
            "Bước tiếp theo: sửa JSON trong editor (nội dung bạn nhập vẫn còn) rồi bấm lưu "
            "lại. Accepted state không đổi."
        )
        return
    intent = (chapter_id, _chapter_ui.payload_fingerprint(payload))
    try:
        blocked, result = _chapter_ui.run_action(
            KEY_EDIT_JSON,
            intent=intent,
            project=project,
            call=lambda: reconcile.edit_reconciliation_candidate(
                project,
                chapter_id=chapter_id,
                payload=payload,
                operation_id=_chapter_ui.stable_operation_id("reconcile", "edit_json", *intent),
            ),
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Sửa đúng field/ID được nêu ở trên rồi lưu lại. Chapter vẫn `finalizing`, "
                "timeline/relationship chưa đổi."
            ),
        )
        return
    if blocked:
        return
    set_action_result(result)
    _common.clear_session_keys(key, f"{key}__source")
    st.rerun()


def _render_actions_panel(ctx: AppContext, chapter_id: str) -> None:
    import streamlit as st

    project = ctx.project
    assert project is not None
    st.subheader("Action reconciliation")
    attempt = _chapter_ui.attempt_input(KEY_ATTEMPT)

    llm_ready = ctx.llm_client is not None and ctx.registry is not None
    generate_col, retry_col = st.columns(2)
    with generate_col:
        generate_clicked = st.button(
            "Generate reconciliation (LLM)", key=KEY_GENERATE, disabled=not llm_ready
        )
    with retry_col:
        retry_clicked = st.button(
            "Retry reconcile (idempotent, không generate lại prose)",
            key=KEY_RETRY,
            disabled=not llm_ready,
        )
    if not llm_ready:
        st.error(
            "Chưa dùng được action gọi LLM: "
            + ("thiếu prompt registry. " if ctx.registry is None else "")
            + ("chưa dựng được LLM client. " if ctx.llm_client is None else "")
            + (ctx.llm_error or "")
        )

    if generate_clicked or retry_clicked:
        # Nút `disabled` chỉ là guard ở UI; bàn phím/AppTest vẫn kích hoạt được, nên
        # backend phải tự chặn. Trước đây chỗ này là `assert`, làm page crash
        # traceback khi chưa dựng được LLM client thay vì báo lỗi cấu hình.
        client = ctx.llm_client
        if client is None:
            st.error(
                "Chưa dựng được LLM client nên không chạy được action này. "
                "Kiểm tra cấu hình LLM (endpoint/model/API key) rồi thử lại."
            )
            return
        action = "generate" if generate_clicked else "retry"
        intent = (chapter_id, action, int(attempt))
        operation_id = _chapter_ui.stable_operation_id("reconcile", *intent)
        try:
            if generate_clicked:
                blocked, result = _chapter_ui.run_action(
                    KEY_GENERATE,
                    intent=intent,
                    project=project,
                    call=lambda: reconcile.generate_reconciliation(
                        project,
                        client=client,
                        chapter_id=chapter_id,
                        operation_id=operation_id,
                    ),
                )
            else:
                blocked, result = _chapter_ui.run_action(
                    KEY_RETRY,
                    intent=intent,
                    project=project,
                    call=lambda: reconcile.retry_reconcile(
                        project,
                        client=client,
                        chapter_id=chapter_id,
                        operation_id=operation_id,
                    ),
                )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Chapter vẫn `finalizing` và chương sau vẫn khóa. Thử lại, sửa JSON tay "
                    "nếu đã có proposal, hoặc Cancel finalizing."
                ),
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()

    accept_col, reject_col, cancel_col = st.columns(3)
    with accept_col:
        accept_clicked = st.button("Accept reconciliation (commit)", key=KEY_ACCEPT)
    with reject_col:
        reject_clicked = st.button("Reject proposal", key=KEY_REJECT)
    with cancel_col:
        cancel_clicked = st.button("Cancel finalizing", key=KEY_CANCEL)

    if reject_clicked:
        intent = (chapter_id, "reject")
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_REJECT,
                intent=intent,
                project=project,
                call=lambda: reconcile.reject_reconciliation(
                    project,
                    chapter_id=chapter_id,
                    operation_id=_chapter_ui.stable_operation_id("reconcile", "reject", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step="Kiểm tra lại proposal; chapter vẫn `finalizing` nếu reject lỗi.",
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()
    if cancel_clicked:
        intent = (chapter_id, "cancel")
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_CANCEL,
                intent=intent,
                project=project,
                call=lambda: reconcile.cancel_finalizing(
                    project,
                    chapter_id=chapter_id,
                    operation_id=_chapter_ui.stable_operation_id("reconcile", "cancel", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step="Chapter có thể không còn `finalizing`; đọc lại trạng thái ở trên.",
            )
        else:
            if blocked:
                return
            set_action_result(result)
            st.rerun()
    if accept_clicked:
        info = _chapter_ui.pending_view(project, chapter_id) or {}
        intent = (
            chapter_id,
            info.get("candidate_revision"),
            info.get("accepted_revision"),
        )
        try:
            blocked, result = _chapter_ui.run_action(
                KEY_ACCEPT,
                intent=intent,
                project=project,
                call=lambda: reconcile.accept_reconciliation(
                    project,
                    chapter_id=chapter_id,
                    operation_id=_chapter_ui.stable_operation_id("reconcile", "accept", *intent),
                ),
            )
        except ServiceError as error:
            _common.render_service_error(
                error,
                next_step=(
                    "Nếu là freshness/validation: sửa proposal hoặc retry để tạo proposal "
                    "mới. Timeline/relationship chưa đổi; chương sau vẫn khóa."
                ),
            )
            return
        if blocked:
            return
        set_action_result(result)
        _chapter_ui.set_note("reconcile", _commit_summary(project, chapter_id, result))
        st.rerun()


def _commit_summary(project: Any, chapter_id: str, result: Any) -> str:
    """Tóm tắt state mới sau commit để hiển thị ở đầu page sau rerun."""
    chapter = _chapter_ui.chapter_by_id(project, chapter_id)
    try:
        timeline = storage.load_timeline(project)
        timeline_text = (
            f"{len(timeline.entries)} entry · latest_consistent_chapter "
            f"{timeline.latest_consistent_chapter}"
        )
    except storage.StorageError:  # pragma: no cover
        timeline_text = "không đọc được"
    try:
        relationships = storage.load_relationships(project)
        relationship_text = (
            f"{len(relationships.relationships)} state · latest_consistent_chapter "
            f"{relationships.latest_consistent_chapter}"
        )
    except storage.StorageError:  # pragma: no cover
        relationship_text = "không đọc được"
    final_revision = (
        chapter.final_revision.revision
        if chapter is not None and chapter.final_revision is not None
        else "—"
    )
    ids = list(getattr(result, "data", {}).get("relationship_ids") or [])
    parts = [
        f"`{chapter_id}` → `{chapter.status.value if chapter else '?'}` · final r{final_revision}",
        f"timeline: {timeline_text}",
        f"relationships: {relationship_text}",
    ]
    if ids:
        parts.append("relationship cập nhật: " + ", ".join(ids))
    return "Đã commit reconciliation — " + " · ".join(parts)

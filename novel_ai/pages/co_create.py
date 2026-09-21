"""Workspace Co-create: chat working state và Finalize Base Idea (T20).

Page này hiện thực `docs/design/workflow.md` mục 4.1 và `novel_ai_spec_v0.2.md`
mục 32: AI chỉ cập nhật **working state** (`co_create.json`); canon chỉ đổi khi
user bấm **Finalize Idea**, lúc đó service ghi `idea/base_idea.md` + metadata và
đặt `co_create.status = finalized`.

Luật đã giữ trong page:

- mọi mutation (`co_create.run_turn`, `co_create.set_working_state`,
  `co_create.finalize_base_idea`) nằm sau `st.form_submit_button`/`st.button`
  trong nhánh `if submitted:`; rerun thuần không gọi LLM và không ghi file;
- page không tự sửa state khi service báo lỗi; dữ liệu user đang sửa được giữ
  trong `st.session_state` để chỉnh tiếp;
- UI nói rõ working state **chưa** là canon, và Base Idea đã chốt được đọc lại
  từ file (`idea/base_idea.md`) + metadata, không suy từ session state.
"""

from __future__ import annotations

from typing import Any

from novel_ai.core import storage
from novel_ai.core.models import (
    ArtifactStatus,
    CoCreateDocument,
    CoCreateStatus,
    IdeaState,
)
from novel_ai.services import ServiceError, co_create
from novel_ai.ui import page_header, set_action_result, show_action_result
from novel_ai.ui.layout import AppContext

from . import _common

__all__ = ["build_working_state_inputs", "render"]

#: Khóa session state của page (chỉ UI working state, không phải canon).
KEY_TURN_MESSAGE = "novel_ai_co_create_message"
KEY_FINALIZE_MARKDOWN = "novel_ai_co_create_finalize_markdown"

#: Khóa widget biểu mẫu working state; user bấm "Nạp lại" thì các khóa này bị xoá.
_KEY_FIELDS: tuple[str, ...] = (
    "genre",
    "core_concept",
    "tone",
    "protagonist",
    "setting",
    "conflict",
    "constraints",
    "open_questions",
)


def _field_key(field: str) -> str:
    return f"novel_ai_co_create_field_{field}"


# ---------------------------------------------------------------------------
# Helper thuần
# ---------------------------------------------------------------------------


def build_working_state_inputs(values: dict[str, Any]) -> dict[str, Any]:
    """Dựng payload `IdeaState` từ giá trị form của user.

    Giá trị rỗng được giữ nguyên (không tự bịa nội dung) để service validate và
    UI hiển thị lỗi theo field. Hàm thuần, tách khỏi Streamlit để test được.
    """
    return _common.working_state_from_values(
        genre=values.get("genre", ""),
        core_concept=values.get("core_concept", ""),
        tone=values.get("tone", ""),
        protagonist=values.get("protagonist", ""),
        setting=values.get("setting", ""),
        conflict=values.get("conflict", ""),
        constraints=values.get("constraints", ""),
        open_questions=values.get("open_questions", ""),
    )


def _idea_state_values(idea_state: IdeaState | None) -> dict[str, Any]:
    if idea_state is None:
        return {
            "genre": "",
            "core_concept": "",
            "tone": "",
            "protagonist": "",
            "setting": "",
            "conflict": "",
            "constraints": "",
            "open_questions": "",
        }
    return {
        "genre": idea_state.genre,
        "core_concept": idea_state.core_concept,
        "tone": idea_state.tone,
        "protagonist": idea_state.protagonist,
        "setting": idea_state.setting,
        "conflict": idea_state.conflict,
        "constraints": "\n".join(idea_state.constraints),
        "open_questions": "\n".join(idea_state.open_questions),
    }


def _document(project: Any) -> CoCreateDocument:
    document = storage.load_co_create(project)
    return document if document is not None else CoCreateDocument()


def _read_base_idea(project: Any) -> tuple[str, Any]:
    """Đọc Base Idea đã chốt từ file: `(markdown, metadata)` (không suy từ state)."""
    document = _document(project)
    meta = document.base_idea
    if not project.paths.base_idea_md.is_file():
        return "", meta
    try:
        return storage.read_text(project.paths.base_idea_md), meta
    except Exception as exc:  # pragma: no cover - file hỏng thì UI báo thật
        return f"(không đọc được `idea/base_idea.md`: {type(exc).__name__})", meta


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render(ctx: AppContext) -> None:
    import streamlit as st

    page_header(
        "Co-create",
        "Working state không phải canon: chỉ Finalize Idea mới ghi `idea/base_idea.md`.",
    )
    show_action_result()

    project = ctx.project
    if project is None:
        st.info("Chọn hoặc tạo project trước khi dùng Co-create.")
        return

    document = _document(project)
    base_idea_markdown, base_idea_meta = _read_base_idea(project)

    _render_status(document, base_idea_meta)
    _render_chat(ctx, document)
    _render_working_state(ctx, document)
    _render_finalize(ctx, document, base_idea_meta)
    _render_base_idea(base_idea_markdown, base_idea_meta, document)


def _render_status(document: CoCreateDocument, base_idea_meta: Any) -> None:
    import streamlit as st

    finalized = document.status is CoCreateStatus.finalized
    st.caption(
        f"Trạng thái co-create: `{document.status.value}` · "
        f"{len(document.messages)} message · working state "
        + ("đã được chốt thành Base Idea" if finalized else "**chưa** là canon")
    )
    if base_idea_meta is not None:
        st.caption(
            f"Base Idea: status `{base_idea_meta.status.value}` · "
            f"revision r{base_idea_meta.revision} · "
            f"accepted_by `{base_idea_meta.accepted_by or '—'}` · "
            f"markdown_ref `{base_idea_meta.markdown_ref}`"
        )
    st.warning(
        "Working state (genre/tone/nhân vật/xung đột…) là **bản nháp làm việc**. "
        "Architect chỉ dùng Base Idea đã accepted; sửa working state không tự đổi Base Idea."
    )
    if finalized:
        st.info(
            "Co-create đã `finalized` nên lượt chat tiếp theo bị guard chặn. "
            "Muốn trao đổi tiếp: lưu working state bên dưới (action reopen tường minh)."
        )


def _render_chat(ctx: AppContext, document: CoCreateDocument) -> None:
    import streamlit as st

    st.subheader("Hội thoại Co-create")
    if document.messages:
        for message in document.messages[-12:]:
            role = {"user": "Bạn", "assistant": "AI", "system": "Hệ thống"}.get(
                message.role, message.role
            )
            st.markdown(f"**{role}:** {message.content}")
    else:
        st.caption("Chưa có lượt nào. Gửi tin nhắn đầu tiên để AI dựng `idea_state`.")

    if ctx.llm_client is None:
        st.error(
            "Chưa dựng được LLM client nên không gửi được lượt co-create. "
            + (ctx.llm_error or "Kiểm tra cấu hình LLM (`NOVEL_AI_*`).")
        )
        return
    if ctx.registry is None:
        st.error(
            "Chưa load được prompt registry v1 nên không render được prompt co-create. "
            "Bước tiếp theo: kiểm tra `docs/prompts/v1/manifest.json`."
        )
        return

    with st.form("novel_ai_co_create_turn"):
        message = st.text_area(
            "Tin nhắn của bạn",
            key=KEY_TURN_MESSAGE,
            placeholder="Ví dụ: Tôi muốn truyện tiên hiệp hài, nhân vật chính là bác sĩ cấp cứu…",
        )
        submitted = st.form_submit_button("Gửi lượt co-create")
    if not submitted:
        return
    if not str(message or "").strip():
        st.error("Cần nhập tin nhắn trước khi gửi lượt co-create.")
        return
    try:
        result = co_create.run_turn(
            ctx.project, client=ctx.llm_client, user_message=str(message)
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Đọc lại lỗi trên, chỉnh tin nhắn rồi gửi lại. Working state và Base Idea "
                "accepted không bị thay đổi bởi lần gửi lỗi."
            ),
        )
        return
    set_action_result(result)
    st.rerun()


def _render_working_state(ctx: AppContext, document: CoCreateDocument) -> None:
    import streamlit as st

    st.subheader("Working idea state (sửa tay)")
    st.caption(
        "Sửa tay cũng là action **reopen** tường minh: lưu xong `co_create.status` "
        "trở lại `working`. Base Idea đã accepted chỉ đổi khi bạn Finalize lại."
    )
    if ctx.project is None:
        return
    idea_state = document.idea_state
    if st.button("Nạp lại biểu mẫu từ working state", key="novel_ai_co_create_reload"):
        _common.clear_session_keys(*(_field_key(field) for field in _KEY_FIELDS))
        st.rerun()

    values = _idea_state_values(idea_state)
    with st.form("novel_ai_co_create_working_state"):
        genre = st.text_input(
            "Genre (bắt buộc)", value=values["genre"], key=_field_key("genre")
        )
        core_concept = st.text_area(
            "Hạt nhân truyện (bắt buộc)",
            value=values["core_concept"],
            height=90,
            key=_field_key("core_concept"),
        )
        tone = st.text_input("Tone", value=values["tone"], key=_field_key("tone"))
        protagonist = st.text_input(
            "Nhân vật trung tâm", value=values["protagonist"], key=_field_key("protagonist")
        )
        setting = st.text_input("Bối cảnh", value=values["setting"], key=_field_key("setting"))
        conflict = st.text_input(
            "Xung đột chính", value=values["conflict"], key=_field_key("conflict")
        )
        constraints = st.text_area(
            "Ràng buộc đã chốt (mỗi dòng một ý)",
            value=values["constraints"],
            height=90,
            key=_field_key("constraints"),
        )
        open_questions = st.text_area(
            "Điều còn mở (mỗi dòng một ý)",
            value=values["open_questions"],
            height=90,
            key=_field_key("open_questions"),
        )
        submitted = st.form_submit_button("Lưu working state")
    if not submitted:
        return
    payload = build_working_state_inputs(
        {
            "genre": genre,
            "core_concept": core_concept,
            "tone": tone,
            "protagonist": protagonist,
            "setting": setting,
            "conflict": conflict,
            "constraints": constraints,
            "open_questions": open_questions,
        }
    )
    try:
        result = co_create.set_working_state(ctx.project, idea_state=payload)
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Điền `genre` và `core_concept` rồi lưu lại; dữ liệu bạn vừa nhập vẫn "
                "còn trong biểu mẫu."
            ),
        )
        return
    set_action_result(result)
    st.rerun()


def _render_finalize(
    ctx: AppContext, document: CoCreateDocument, base_idea_meta: Any
) -> None:
    import streamlit as st

    st.subheader("Finalize Idea")
    st.caption(
        "Finalize ghi `idea/base_idea.md` + `idea/base_idea.meta.json` và đặt "
        "`co_create.status = finalized`. Gửi lại **cùng nội dung** là no-op; nội dung "
        "khác sẽ bump revision và đánh dấu stale downstream liên quan (không tự rewrite)."
    )
    if base_idea_meta is not None and base_idea_meta.status is ArtifactStatus.accepted:
        st.caption(
            f"Đang có Base Idea accepted r{base_idea_meta.revision}. Finalize lại = revise upstream."
        )
    with st.form("novel_ai_co_create_finalize"):
        markdown = st.text_area(
            "Markdown Base Idea (tuỳ chọn)",
            key=KEY_FINALIZE_MARKDOWN,
            height=160,
            placeholder=(
                "Bỏ trống để dùng working state hiện tại; hoặc dán Base Idea do bạn tự viết."
            ),
        )
        submitted = st.form_submit_button("Finalize Idea")
    if not submitted:
        return
    project = ctx.project
    if project is None:  # pragma: no cover - đã chặn ở render
        return
    try:
        result = co_create.finalize_base_idea(
            project, markdown=str(markdown or "").strip() or None
        )
    except ServiceError as error:
        _common.render_service_error(
            error,
            next_step=(
                "Nhập markdown Base Idea, hoặc lưu working state có `genre` + "
                "`core_concept`, rồi Finalize lại. Working state giữ nguyên."
            ),
        )
        return
    set_action_result(result)
    st.rerun()


def _render_base_idea(
    markdown: str, base_idea_meta: Any, document: CoCreateDocument
) -> None:
    import streamlit as st

    st.subheader("Base Idea đã chốt (canon)")
    if not markdown:
        st.info(
            "Chưa có `idea/base_idea.md`. Bước tiếp theo: Finalize Idea để mở khoá Architect."
        )
        return
    if base_idea_meta is not None:
        st.caption(
            f"status `{base_idea_meta.status.value}` · revision r{base_idea_meta.revision} · "
            f"accepted_at `{base_idea_meta.accepted_at or '—'}`"
        )
    st.markdown(markdown)
    with st.expander("Xem markdown thô + working state hiện tại"):
        st.code(markdown, language="markdown")
        if document.idea_state is not None:
            st.caption("Working state (chưa phải canon):")
            st.json(document.idea_state.model_dump(mode="json"))

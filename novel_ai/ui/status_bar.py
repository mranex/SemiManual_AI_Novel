"""Status bar: cấu hình, kết nối LLM, project/chapter/revision, trạng thái artifact (T19).

Nguyên tắc: UI **không** được giả "Connected". Chỉ khi có kết quả probe thật
(thành công/thất bại) mới hiển thị `connected`/`error`; còn lại là
`not_configured` hoặc `configured (chưa kiểm tra)`. Module này không tự gọi
mạng: `probe_llm` là action rõ ràng do người dùng bấm, và mọi lỗi được trả về
dưới dạng trạng thái, không raise vào UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from novel_ai.config import AppConfig
from novel_ai.core import storage
from novel_ai.core.models import ChapterStatus
from novel_ai.core.project import Project


class LLMConnectivity(str, Enum):
    """Trạng thái kết nối provider. Không suy diễn, không giả Connected."""

    fake = "fake"
    not_configured = "not_configured"
    configured = "configured"
    checking = "checking"
    connected = "connected"
    error = "error"


_LLM_LABEL: dict[LLMConnectivity, str] = {
    LLMConnectivity.fake: "Fake LLM (offline)",
    LLMConnectivity.not_configured: "Chưa cấu hình API",
    LLMConnectivity.configured: "Đã cấu hình (chưa kiểm tra kết nối)",
    LLMConnectivity.checking: "Đang kiểm tra kết nối…",
    LLMConnectivity.connected: "Đã kết nối",
    LLMConnectivity.error: "Lỗi kết nối",
}


@dataclass
class StatusBar:
    project_id: str
    project_title: str
    workspace: str = ""
    language: str = "vi"
    genre_prompt_id: str = "custom"
    writing_style_id: str = "default"
    auto_accept_structured: bool = False
    allow_relationship_replan: bool = True
    rolling_plan_every: int = 3
    current_chapter: int = 1
    chapter_label: str = ""
    prose_revision_label: str = ""
    artifact_states: dict[str, str] = field(default_factory=dict)
    config_warnings: list[str] = field(default_factory=list)
    llm_status: LLMConnectivity = LLMConnectivity.configured
    llm_message: str = ""
    needs_recovery: bool = False
    read_only: bool = False

    @property
    def llm_label(self) -> str:
        return _LLM_LABEL[self.llm_status]

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_title": self.project_title,
            "workspace": self.workspace,
            "current_chapter": self.current_chapter,
            "chapter_label": self.chapter_label,
            "prose_revision_label": self.prose_revision_label,
            "artifact_states": dict(self.artifact_states),
            "llm_status": self.llm_status.value,
            "llm_label": self.llm_label,
            "llm_message": self.llm_message,
            "config_warnings": list(self.config_warnings),
            "needs_recovery": self.needs_recovery,
            "read_only": self.read_only,
        }


def llm_status_from_config(config: AppConfig) -> tuple[LLMConnectivity, str]:
    """Trạng thái LLM chỉ từ cấu hình — không gọi mạng, không giả Connected."""
    if config.use_fake_llm:
        return LLMConnectivity.fake, "Sinh văn bản bằng fake client offline."
    missing = [
        name
        for name, value in (
            ("NOVEL_AI_API_BASE_URL", config.api_base_url),
            ("NOVEL_AI_API_KEY", config.api_key),
            ("NOVEL_AI_MODEL", config.model),
        )
        if not value
    ]
    if missing:
        return (
            LLMConnectivity.not_configured,
            "Thiếu cấu hình: " + ", ".join(missing) + ". Sửa `.env` rồi khởi động lại app.",
        )
    return (
        LLMConnectivity.configured,
        "Chưa kiểm tra kết nối. Bấm “Kiểm tra kết nối” nếu muốn xác nhận.",
    )


def build_status_bar(
    project: Project,
    *,
    config: AppConfig | None = None,
    workspace: str = "",
    chapter_id: str | None = None,
    llm_status: LLMConnectivity | None = None,
    llm_message: str = "",
) -> StatusBar:
    """Dựng dữ liệu status bar từ project + app config (không mutate)."""
    project_config = project.config
    resolved_status: LLMConnectivity
    resolved_message = llm_message
    if llm_status is None:
        if config is not None:
            resolved_status, resolved_message = llm_status_from_config(config)
        else:
            resolved_status = LLMConnectivity.configured
    else:
        resolved_status = llm_status

    chapter_label = ""
    prose_label = ""
    target_chapter = chapter_id
    if target_chapter is None:
        for candidate in storage.list_chapter_ids(project):
            chapter = storage.load_chapter(project, candidate)
            if chapter is None:
                continue
            if chapter.status is not ChapterStatus.final_reconciled:
                target_chapter = candidate
                break
    if target_chapter is not None:
        chapter = storage.load_chapter(project, target_chapter)
        if chapter is not None:
            chapter_label = f"Ch.{chapter.chapter_number} — {chapter.title} ({chapter.status.value})"
            if chapter.current_draft_revision is not None:
                draft = chapter.current_draft
                suffix = "" if draft is None or draft.is_complete else " · partial"
                prose_label = f"prose r{chapter.current_draft_revision}{suffix}"

    from novel_ai.ui.project_tree import artifact_status_map

    warnings = list(config.warnings()) if config is not None else []
    return StatusBar(
        project_id=project_config.project_id,
        project_title=project_config.title,
        workspace=workspace,
        language=project_config.default_language,
        genre_prompt_id=project_config.genre_prompt_id,
        writing_style_id=project_config.writing_style_id,
        auto_accept_structured=project_config.auto_accept_structured,
        allow_relationship_replan=project_config.allow_relationship_replan,
        rolling_plan_every=project_config.rolling_plan_every,
        current_chapter=project_config.current_chapter,
        chapter_label=chapter_label,
        prose_revision_label=prose_label,
        artifact_states=artifact_status_map(project),
        config_warnings=warnings,
        llm_status=resolved_status,
        llm_message=resolved_message,
        needs_recovery=storage.needs_recovery(project),
        read_only=storage.requires_manual_recovery(project),
    )


def render(status: StatusBar) -> None:
    """Hiển thị status bar (Streamlit). Không mutate, không gọi API."""
    import streamlit as st

    left, middle, right = st.columns([2, 2, 2])
    with left:
        st.caption(
            f"**{status.project_title}** · `{status.project_id}` · "
            f"{status.workspace or 'chưa chọn workspace'}"
        )
        st.caption(
            f"{status.chapter_label or 'chưa có chapter đang làm'} · "
            f"{status.prose_revision_label or 'chưa có prose revision'}"
        )
    with middle:
        st.caption(
            f"Ngôn ngữ: {status.language} · genre: `{status.genre_prompt_id}` · "
            f"style: `{status.writing_style_id}`"
        )
        st.caption(
            "Auto Accept structured: "
            + ("BẬT (prose/Human Review vẫn cần người dùng)" if status.auto_accept_structured else "tắt")
            + f" · relationship replan: {'cho phép' if status.allow_relationship_replan else 'khóa'}"
        )
    with right:
        if status.llm_status is LLMConnectivity.connected:
            st.success(f"LLM: {status.llm_label}")
        elif status.llm_status is LLMConnectivity.error:
            st.error(f"LLM: {status.llm_label} — {status.llm_message}")
        elif status.llm_status in (LLMConnectivity.not_configured, LLMConnectivity.fake):
            st.warning(f"LLM: {status.llm_label} — {status.llm_message}")
        else:
            st.info(f"LLM: {status.llm_label} — {status.llm_message}")

    if status.read_only:
        st.error(
            "Project đang read-only vì có transaction không tự recovery được. "
            "Vào workspace Revision/Recovery để xử lý trước khi ghi tiếp."
        )
    elif status.needs_recovery:
        st.warning("Có transaction dở; backend sẽ recovery trước khi cho ghi tiếp.")
    for message in status.config_warnings:
        st.warning(message)

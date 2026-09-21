"""Streamlit shell, router workspace và action cấp app (T19, layout 3 vùng T25).

Module này là **entrypoint thật** của app: nó ghép `novel_ai.ui.project_tree`,
`novel_ai.ui.status_bar` và `novel_ai.ui.arbiter` thành một shell hoàn chỉnh, và
định nghĩa hợp đồng router để T20–T22 nối các workspace vào.

Bố cục bám `novel_ai_spec_v0.2.md` mục 28: nav workspace ở **đỉnh**, ba vùng
`PROJECT | CURRENT WORKSPACE | ARBITER`, status bar ở **đáy**. Sidebar của
Streamlit chỉ còn là nơi chọn/tạo project vì không thể đặt pane tùy ý trong lưới
cột; cây project là pane trái thật trong lưới.

Luật không được nới trong file này (D011, `docs/design/architecture.md` mục 4):

- State bền nằm ở **file project**. `st.session_state` chỉ giữ UI working state:
  slug project đang mở, workspace đang chọn, node đang chọn, kết quả probe LLM.
- Mọi mutation (tạo project, recovery) nằm sau `st.form_submit_button`/
  `st.button` trong nhánh `if submitted:`. Rerun thuần **không** ghi file và
  **không** phát API call.
- UI **không** giả "Connected": chỉ khi `probe_llm` trả kết quả thật mới hiển thị
  `connected`/`error`.
- Workspace chưa implement phải nói thật là chưa nối, không giả hoạt động.
- UI không import cứng page: `render_workspace` import lazy để T20–T22 thêm
  module mà không phải sửa file này. UI cũng không gọi service nào của T13–T18 ở
  đây; page của T20+ chịu trách nhiệm gọi service cho mutation của nó.

Module này **không** tự đọc/ghi file project: nó dùng `core.project` (tạo/mở,
liệt kê), `core.storage` (chỉ cho recovery theo architecture.md mục 7) và các
builder thuần của UI. Tạo project dùng `Project.create` của T09 vì đó là
layout/metadata thuộc core; guard ghi thật vẫn nằm ở backend (D011).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from novel_ai.config import AppConfig, get_config
from novel_ai.core.llm import (
    ChatMessage,
    FakeLLMClient,
    LLMClient,
    LLMError,
    LLMRequest,
    OpenAICompatibleClient,
    llm_config_from_app_config,
    redact_secrets,
    validate_llm_config,
)
from novel_ai.core.project import Project, list_projects
from novel_ai.core.prompts import PromptError, PromptRegistry
from novel_ai.ui import arbiter, project_tree, status_bar

__all__ = [
    "AppContext",
    "NAV_LABELS",
    "WORKSPACE_MODULES",
    "WORKSPACE_NOT_WIRED_TEMPLATE",
    "build_llm_client",
    "label_for_workspace",
    "load_prompt_registry",
    "main_css",
    "nav_labels",
    "probe_llm",
    "render_arbiter_panel",
    "render_recovery_banner",
    "render_workspace",
    "run",
    "select_project",
    "workspace_from_label",
]

#: Tiêu đề trang Streamlit; giữ khớp entrypoint cũ (T07) để test/URL ổn định.
PAGE_TITLE = "Manual AI Novel"
PAGE_ICON = "📕"

#: Nhãn nav ngắn ở đỉnh (spec mục 28). Hai workspace vận hành
#: (`reconcile`, `revision`) không có trong sơ đồ 7 mục của spec nên lấy nhãn
#: từ `project_tree.WORKSPACES` để không bịa tên mới.
NAV_LABELS: dict[str, str] = {
    "co_create": "Co-create",
    "architect": "Architect",
    "long_plan": "Long Plan",
    "short_plan": "Short Plan",
    "skeleton": "Skeleton",
    "writer": "Writer",
    "review": "Review",
}

#: Khóa `st.session_state` cho UI working state (không phải state bền của truyện).
KEY_OPEN_PROJECT = "novel_ai_open_project"
KEY_WORKSPACE = "novel_ai_workspace"
KEY_WORKSPACE_NAV = "novel_ai_workspace_nav"
KEY_TREE_NODE = "novel_ai_tree_node"
KEY_LLM_STATUS = "novel_ai_llm_status"
KEY_LLM_MESSAGE = "novel_ai_llm_message"

#: Workspace id -> module trong `novel_ai.pages`.
#:
#: T19 tạo router này nhưng **chưa** tạo page: T20–T22 nối `co_create`, `architect`,
#: `long_plan`, `short_plan`, `skeleton`, `writer`, `review`, `reconcile`,
#: `revision`. Module nào chưa tồn tại thì `render_workspace` báo thật là chưa nối.
#:
#: Quy ước cho page (T20+): module phải có `render(ctx: AppContext) -> None`, chỉ
#: gọi service cho mutation, và mọi action ghi dữ liệu nằm sau
#: `st.button`/`st.form_submit_button` trong nhánh `if submitted:`.
WORKSPACE_MODULES: dict[str, str] = {
    "co_create": "novel_ai.pages.co_create",
    "architect": "novel_ai.pages.architect",
    "long_plan": "novel_ai.pages.long_plan",
    "short_plan": "novel_ai.pages.short_plan",
    "skeleton": "novel_ai.pages.skeleton",
    "writer": "novel_ai.pages.writer",
    "review": "novel_ai.pages.review",
    "reconcile": "novel_ai.pages.reconcile",
    "revision": "novel_ai.pages.revision",
}

#: Thông báo trung thực cho workspace chưa được nối (không giả hoạt động).
WORKSPACE_NOT_WIRED_TEMPLATE = (
    "Workspace này sẽ được nối ở T20/T21/T22; chưa có hành vi nào chạy được."
)


@dataclass(frozen=True)
class AppContext:
    """Bối cảnh một lần render, truyền cho mọi page workspace.

    `registry=None` nghĩa là prompt manifest v1 chưa load được (xem
    `load_prompt_registry`) — page phải báo lỗi cấu hình thay vì crash.
    `llm_client=None` (kèm `llm_error`) nghĩa là chưa dựng được client; page
    không được gọi LLM trong trường hợp đó.
    """

    app_config: AppConfig
    project: Project | None
    registry: PromptRegistry | None
    workspace: str
    llm_client: LLMClient | None
    llm_error: str | None


def build_llm_client(
    app_config: AppConfig, *, force_fake: bool = False
) -> tuple[LLMClient | None, str | None]:
    """Dựng LLM client từ app config; **không** gọi mạng.

    Trả `(client, error)`:

    - `use_fake_llm` (hoặc `force_fake=True`) -> `FakeLLMClient`, `error=None`;
    - ngược lại dựng `OpenAICompatibleClient` sau khi `validate_llm_config`;
    - mọi lỗi cấu hình được trả về dưới dạng message tiếng Việt (nêu **tên biến
      môi trường**, không nêu giá trị secret) và `client=None`. Hàm không raise
      vào UI và không gửi request nào.
    """
    if force_fake or app_config.use_fake_llm:
        return FakeLLMClient(), None
    llm_config = llm_config_from_app_config(app_config)
    problems = validate_llm_config(llm_config)
    if problems:
        return None, " ".join(problems)
    try:
        return OpenAICompatibleClient(llm_config), None
    except LLMError as exc:  # pragma: no cover - chỉ khi transport bị inject sai
        return None, redact_secrets(str(exc), llm_config)
    except Exception as exc:  # pragma: no cover - lỗi cấu hình ngoài dự kiến
        return None, (
            f"Không dựng được LLM client ({type(exc).__name__}); kiểm tra cấu hình LLM."
        )


def load_prompt_registry(
    app_config: AppConfig,
) -> tuple[PromptRegistry | None, str | None]:
    """Load prompt registry v1; trả `(registry, error)` thay vì raise vào UI."""
    try:
        registry = PromptRegistry.load(
            app_config.prompt_root, repo_root=app_config.repo_root
        )
    except PromptError as exc:
        return None, f"Không load được prompt registry v1: {exc}"
    except OSError as exc:  # pragma: no cover - lỗi FS khi đọc manifest
        return None, f"Không đọc được prompt manifest: {type(exc).__name__}."
    return registry, None


def probe_llm(client: LLMClient, *, prompt: str = "ping") -> tuple[bool, str]:
    """Gọi provider một lần bằng action rõ ràng của người dùng.

    Đây là **action**, không phải render: chỉ được gọi trong nhánh
    `if st.button(...)`. Kết quả trả về `(ok, message)` để UI ghi vào session
    state; không raise vào UI.
    """
    request = LLMRequest(messages=(ChatMessage(role="user", content=str(prompt)),))
    try:
        client.complete(request)
    except LLMError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - provider lỗi ngoài dự kiến
        return False, f"Lỗi không mong đợi khi kiểm tra kết nối ({type(exc).__name__})."
    return True, "Đã kết nối provider (kiểm tra thật bằng một request nhỏ)."


def select_project(app_config: AppConfig) -> Project | None:
    """Sidebar: tạo project mới hoặc mở project có sẵn.

    Chỉ mutate khi người dùng bấm nút submit. Project đang mở được nhớ bằng slug
    trong `st.session_state`; state bền vẫn nằm ở `projects/<slug>/project.json`.
    """
    import streamlit as st

    current = _open_project_from_state(app_config)
    if current is not None:
        st.caption(f"Đang mở: **{current.config.title}** · `{current.slug}`")

    create_tab, open_tab = st.tabs(["Tạo project", "Mở project"])
    with create_tab:
        created = _create_project_form(app_config)
        if created is not None:
            return created
    with open_tab:
        opened = _open_project_form(app_config)
        if opened is not None:
            return opened
    return current


def render_workspace(ctx: AppContext) -> None:
    """Import lazy page của workspace và gọi `render(ctx)`.

    Module chưa tồn tại (T20–T22) -> hiển thị thông báo trung thực, **không** giả
    hoạt động. Module tồn tại nhưng import lỗi hoặc thiếu `render` -> báo lỗi thật
    của module đó thay vì nuốt lỗi.
    """
    import streamlit as st

    module_name = WORKSPACE_MODULES.get(ctx.workspace)
    if module_name is None:
        st.error(
            f"Workspace `{ctx.workspace}` chưa có trong router; "
            "kiểm tra `novel_ai.ui.layout.WORKSPACE_MODULES`."
        )
        return
    if ctx.project is None:
        st.info("Chọn hoặc tạo project trước khi dùng workspace.")
        return
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Chỉ coi là "chưa nối" khi chính module (hoặc package `novel_ai.pages`)
        # vắng mặt; lỗi import bên trong page vẫn phải nổi lên.
        if exc.name in {module_name, "novel_ai.pages"}:
            st.info(WORKSPACE_NOT_WIRED_TEMPLATE)
            return
        raise
    except Exception as exc:  # pragma: no cover - page của T20+ import lỗi
        st.error(
            f"Không import được `{module_name}`: {type(exc).__name__}: {exc}. "
            "Đây là lỗi của workspace đó, không phải lỗi đã được xử lý."
        )
        return
    render = getattr(module, "render", None)
    if not callable(render):
        st.error(
            f"Module `{module_name}` thiếu hàm `render(ctx)` theo hợp đồng router T19."
        )
        return
    render(ctx)


def nav_labels() -> list[str]:
    """Nhãn nav theo đúng thứ tự `project_tree.WORKSPACES` (không bịa workspace)."""
    return [label_for_workspace(key) for key, _label in project_tree.WORKSPACES]


def label_for_workspace(workspace: str) -> str:
    """Nhãn hiển thị của một workspace id (nav ngắn, fallback nhãn đầy đủ)."""
    if workspace in NAV_LABELS:
        return NAV_LABELS[workspace]
    return dict(project_tree.WORKSPACES).get(workspace, workspace)


def workspace_from_label(label: str) -> str:
    """Nhãn nav → workspace id; nhãn lạ trả về workspace đầu tiên."""
    keys = [key for key, _label in project_tree.WORKSPACES]
    labels = nav_labels()
    return keys[labels.index(label)] if label in labels else keys[0]


def main_css() -> str:
    """CSS tối thiểu cho bố cục spec mục 28.

    Chỉ đụng phần khung (ẩn nav mặc định của Streamlit, ghim status bar xuống
    đáy, chừa chỗ cho nó, gọn khoảng cách dọc). Không thêm theme/font ngoài, không
    phụ thuộc class nội bộ nào ngoài `data-testid` ổn định của Streamlit.
    """
    return """
    <style>
      header[data-testid="stHeader"] { display: none; }
      div[data-testid="stDecoration"] { display: none; }
      div[data-testid="stToolbar"] { display: none; }
      .block-container { padding-top: 1.1rem; padding-bottom: 4.75rem; max-width: 100%; }
      .dsh-shell-footer {
        position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000000;
        background: #f3f4f6; color: #111827;
        border-top: 1px solid #d1d5db;
        padding: 0.4rem 1.25rem 0.5rem 1.25rem;
      }
      .dsh-shell-footer p { margin: 0; font-size: 0.82rem; color: #111827; }
      .dsh-shell-footer b { color: #111827; }
      .dsh-shell-footer code { color: #374151; background: #e5e7eb; }
      .dsh-shell-title { font-size: 1.35rem; font-weight: 700; margin: 0 0 0.1rem 0; }
      .dsh-nav-caption { font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase;
        color: #6b7280; margin-bottom: 0.15rem; }
      .dsh-nav-sep { border-bottom: 1px solid #d6d6d6; margin: 0.15rem 0 0.9rem 0; }
      .dsh-pane-title { font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase;
        color: #6b7280; font-weight: 600; margin-bottom: 0.25rem; }
      div[data-testid="stVerticalBlockBorderWrapper"] { border-color: #d6d6d6; }
      .dsh-arbiter-row { font-size: 0.86rem; display: flex; gap: 0.4rem; }
      .dsh-arbiter-mark { width: 1.1rem; display: inline-block; }
    </style>
    """


def _render_navbar() -> str:
    """Nav workspace ở đỉnh + lưu lựa chọn vào `KEY_WORKSPACE`."""
    import streamlit as st

    labels = nav_labels()
    keys = [key for key, _label in project_tree.WORKSPACES]
    current = st.session_state.get(KEY_WORKSPACE)
    current = current if current in keys else keys[0]
    if st.session_state.get(KEY_WORKSPACE_NAV) not in labels:
        st.session_state[KEY_WORKSPACE_NAV] = label_for_workspace(current)
    chosen = st.radio(
        "Workspace",
        labels,
        index=labels.index(st.session_state[KEY_WORKSPACE_NAV]),
        key=KEY_WORKSPACE_NAV,
        horizontal=True,
        label_visibility="collapsed",
    )
    workspace = workspace_from_label(str(chosen))
    st.session_state[KEY_WORKSPACE] = workspace
    # Page đọc `st.session_state["novel_ai_workspace"]` qua `ui.current_workspace()`
    # để scope kết quả action; phải gán trước khi `render_workspace` chạy.
    return workspace


def _render_project_pane(project: Project | None) -> None:
    """Pane trái: PROJECT — cây artifact/chapter + kết nối LLM."""
    import streamlit as st

    st.markdown('<div class="dsh-pane-title">Project</div>', unsafe_allow_html=True)
    if project is None:
        st.caption("Chưa mở project nào. Dùng sidebar để tạo hoặc mở project.")
        return
    st.caption(f"**{project.config.title}** · `{project.slug}` · `{project.config.project_id}`")
    project_tree.render(project, key=KEY_TREE_NODE)


def _render_arbiter_pane(project: Project | None, *, llm_client: LLMClient | None) -> None:
    """Pane phải: ARBITER (spec mục 29). Chỉ đọc state, không gọi LLM."""
    import streamlit as st

    st.markdown('<div class="dsh-pane-title">Arbiter</div>', unsafe_allow_html=True)
    if project is None:
        st.caption("Chưa mở project nên Arbiter chưa có gì để đọc.")
        return
    report = arbiter.analyze(project)
    st.caption(arbiter.summarize(report))
    if report.read_only:
        st.error("Project read-only: cần recovery thủ công trước khi ghi tiếp.")
    elif report.needs_recovery:
        st.warning(f"Pending operation: {', '.join(report.pending_operation_ids)}")
    if report.stale_artifact_ids:
        st.warning("Artifact đang stale: " + ", ".join(report.stale_artifact_ids))
    if report.rolling_due:
        st.info("Đến mốc Rolling Plan review; đây chỉ là nhắc, không tự chạy.")

    st.markdown("**Trạng thái**")
    for label, glyph, text in arbiter.status_rows(project, report):
        st.markdown(
            f'<div class="dsh-arbiter-row"><span class="dsh-arbiter-mark">{glyph}</span>'
            f"<span>{label} — {text}</span></div>",
            unsafe_allow_html=True,
        )

    st.markdown("**Bước tiếp theo**")
    if not report.suggestions:
        st.caption("Không có gợi ý; mọi bước hiện tại đã hoàn tất.")
        return
    workspace_labels = dict(project_tree.WORKSPACES)
    for index, suggestion in enumerate(report.suggestions):
        marker = " · **blocking**" if suggestion.blocking else ""
        workspace_label = workspace_labels.get(suggestion.workspace, suggestion.workspace)
        st.markdown(f"- `{workspace_label}` — {suggestion.label}{marker}  \n  {suggestion.reason}")
        button_key = f"novel_ai_arbiter_open_{index}_{suggestion.code}"
        if st.button(
            f"Chuyển tới {workspace_label}",
            key=button_key,
            disabled=llm_client is None and suggestion.workspace in _LLM_WORKSPACES,
        ):
            st.session_state[KEY_WORKSPACE] = suggestion.workspace
            st.session_state[KEY_WORKSPACE_NAV] = label_for_workspace(suggestion.workspace)
            st.rerun()


#: Workspace cần LLM client mới làm được gì đó (dùng để disable nút điều hướng).
_LLM_WORKSPACES = frozenset(
    {"co_create", "architect", "long_plan", "short_plan", "skeleton", "writer", "review", "reconcile", "revision"}
)


def run() -> None:
    """Entrypoint Streamlit: nav trên, 3 pane giữa, status bar dưới (spec mục 28)."""
    import streamlit as st

    st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="wide")
    st.markdown(main_css(), unsafe_allow_html=True)
    app_config = get_config()
    registry, registry_error = load_prompt_registry(app_config)
    llm_client, llm_error = build_llm_client(app_config)

    project = _open_project_from_state(app_config)

    with st.sidebar:
        st.markdown(f'<div class="dsh-shell-title">{PAGE_TITLE}</div>', unsafe_allow_html=True)
        st.caption("State bền nằm ở file project; UI chỉ giữ working state.")
        project = select_project(app_config)
        _render_llm_block(app_config, llm_client, llm_error)
        if registry_error:
            st.warning(registry_error)
        if llm_error:
            st.warning(llm_error)

    if project is None:
        st.session_state.pop(KEY_OPEN_PROJECT, None)
    elif project.root.name != st.session_state.get(KEY_OPEN_PROJECT):
        st.session_state[KEY_OPEN_PROJECT] = project.root.name

    st.markdown('<div class="dsh-nav-caption">Workspace</div>', unsafe_allow_html=True)
    workspace = _render_navbar()
    st.markdown('<div class="dsh-nav-sep"></div>', unsafe_allow_html=True)

    if project is not None:
        render_recovery_banner(project)

    project_pane, workspace_pane, arbiter_pane = st.columns(
        [1.05, 2.1, 1.15], gap="medium"
    )
    with project_pane:
        _render_project_pane(project)
    with workspace_pane:
        st.markdown('<div class="dsh-pane-title">Current workspace</div>', unsafe_allow_html=True)
        st.caption(
            f"`{workspace}` — {label_for_workspace(workspace)}"
            + (f" · {project.config.title}" if project is not None else "")
        )
        if project is None:
            st.info(
                "Chưa có project nào đang mở. Tạo project mới hoặc mở project có sẵn ở "
                "sidebar để bắt đầu."
            )
        else:
            ctx = AppContext(
                app_config=app_config,
                project=project,
                registry=registry,
                workspace=workspace,
                llm_client=llm_client,
                llm_error=llm_error,
            )
            render_workspace(ctx)
    with arbiter_pane:
        _render_arbiter_pane(project, llm_client=llm_client)

    _render_status_bar_footer(
        project,
        config=app_config,
        workspace=workspace,
        llm_client=llm_client,
        llm_error=llm_error,
    )


# ---------------------------------------------------------------------------
# Status bar ở đáy (spec mục 28)
# ---------------------------------------------------------------------------


def _footer_text(project: Project, config: AppConfig, workspace: str) -> str:
    """Một dòng status bar: API/provider/model/context/project (spec mục 28).

    Không tự báo "Connected": chỉ `probe_llm` thật mới set trạng thái đó trong
    session state. Phần đọc state dùng chung `status_bar.build_status_bar`.
    """
    import streamlit as st

    status = status_bar.build_status_bar(
        project,
        config=config,
        workspace=workspace,
        llm_status=st.session_state.get(KEY_LLM_STATUS),
        llm_message=st.session_state.get(KEY_LLM_MESSAGE, ""),
    )
    model = config.model or ("fake (offline)" if config.use_fake_llm else "—")
    if status.read_only:
        api_text = "read-only"
    elif status.needs_recovery:
        api_text = "cần recovery"
    elif status.llm_status is status_bar.LLMConnectivity.connected:
        api_text = "Connected"
    else:
        api_text = status.llm_label
    timeline, _relationship = project_tree.latest_timeline_labels(project)
    return (
        f"API — <b>{api_text}</b> &nbsp;|&nbsp; OpenAI Compatible &nbsp;|&nbsp; "
        f"Model: {model} &nbsp;|&nbsp; Context: state tới {timeline} &nbsp;|&nbsp; "
        f"Project: {status.project_title} (`{status.project_id}`) &nbsp;|&nbsp; "
        f"{status.chapter_label or 'chưa có chapter đang làm'} &nbsp;|&nbsp; "
        f"{status.prose_revision_label or 'chưa có prose revision'}"
    )


def _render_status_bar_footer(
    project: Project | None,
    *,
    config: AppConfig,
    workspace: str,
    llm_client: LLMClient | None,
    llm_error: str | None,
) -> None:
    """Status bar ghim ở đáy trang; chưa mở project thì hiển thị phần cấu hình app."""
    import streamlit as st

    if project is None:
        configured_status, configured_message = status_bar.llm_status_from_config(config)
        st.markdown(
            '<div class="dsh-shell-footer"><p>'
            f"API — <b>{configured_status.value}</b> &nbsp;|&nbsp; OpenAI Compatible "
            f"&nbsp;|&nbsp; Model: {config.model or '—'} &nbsp;|&nbsp; chưa mở project. "
            f"{configured_message}"
            "</p></div>",
            unsafe_allow_html=True,
        )
        return
    st.markdown(
        f'<div class="dsh-shell-footer"><p>{_footer_text(project, config, workspace)}</p></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Project: tạo/mở
# ---------------------------------------------------------------------------


def _open_project_from_state(app_config: AppConfig) -> Project | None:
    """Mở project đã ghi trong session state; slug không còn hợp lệ thì quên đi."""
    import streamlit as st

    slug = st.session_state.get(KEY_OPEN_PROJECT)
    if not slug:
        return None
    try:
        return Project.open(app_config.projects_root, str(slug))
    except Exception:
        st.session_state.pop(KEY_OPEN_PROJECT, None)
        return None


def _create_project_form(app_config: AppConfig) -> Project | None:
    import streamlit as st

    st.caption("Project mới có stable `project_id` và layout T03; slug lấy từ tiêu đề.")
    with st.form("novel_ai_create_project"):
        title = st.text_input("Tiêu đề truyện", key="novel_ai_new_title")
        language = st.text_input(
            "Ngôn ngữ mặc định", value="vi", key="novel_ai_new_language"
        )
        genre = st.text_input("Genre id (prompt)", value="custom", key="novel_ai_new_genre")
        style = st.text_input(
            "Writing style id", value="default", key="novel_ai_new_style"
        )
        submitted = st.form_submit_button("Tạo project")
    if not submitted:
        return None
    if not str(title).strip():
        st.error("Cần nhập tiêu đề truyện trước khi tạo project.")
        return None
    try:
        project = Project.create(
            app_config.projects_root,
            str(title).strip(),
            default_language=str(language).strip() or "vi",
            genre_prompt_id=str(genre).strip() or "custom",
            writing_style_id=str(style).strip() or "default",
        )
    except Exception as exc:
        st.error(
            f"Không tạo được project: {getattr(exc, 'code', type(exc).__name__)} — {exc}"
        )
        return None
    _remember_project(project)
    st.success(f"Đã tạo project `{project.slug}`.")
    return project


def _open_project_form(app_config: AppConfig) -> Project | None:
    import streamlit as st

    warnings: list[str] = []
    try:
        summaries = list_projects(app_config.projects_root, warnings_out=warnings)
    except Exception as exc:  # pragma: no cover - lỗi FS khi đọc projects root
        summaries = []
        warnings.append(
            f"Không đọc được `{app_config.projects_root}`: {type(exc).__name__}."
        )
    for message in warnings:
        st.warning(message)
    if not summaries:
        st.caption(f"Chưa có project nào trong `{app_config.projects_root}`.")
        return None
    labels = {
        str(item["slug"]): f"{item['title']} · {item['slug']} · ch.{item.get('current_chapter', 1)}"
        for item in summaries
    }
    chosen = st.selectbox(
        "Project có sẵn",
        sorted(labels),
        format_func=lambda slug: labels.get(slug, slug),
        key="novel_ai_existing_project",
    )
    if not st.button("Mở project", key="novel_ai_open_button"):
        return None
    try:
        project = Project.open(app_config.projects_root, str(chosen))
    except Exception as exc:
        st.error(f"Không mở được project `{chosen}`: {type(exc).__name__} — {exc}")
        return None
    _remember_project(project)
    return project


def _remember_project(project: Project) -> None:
    """Ghi project đang mở vào UI working state và xóa kết quả probe cũ."""
    import streamlit as st

    st.session_state[KEY_OPEN_PROJECT] = project.slug
    st.session_state.pop(KEY_TREE_NODE, None)
    st.session_state.pop(KEY_LLM_STATUS, None)
    st.session_state.pop(KEY_LLM_MESSAGE, None)


# ---------------------------------------------------------------------------
# Sidebar: LLM
# ---------------------------------------------------------------------------


def _render_llm_block(
    app_config: AppConfig, llm_client: LLMClient | None, llm_error: str | None
) -> None:
    """Action kiểm tra kết nối + ghi kết quả thật vào session state."""
    import streamlit as st

    st.header("Kết nối LLM")
    configured_status, configured_message = status_bar.llm_status_from_config(app_config)
    if configured_status is status_bar.LLMConnectivity.fake:
        st.info("Đang dùng fake LLM offline; render không phát API call nào.")
        return
    if llm_error:
        st.error(llm_error)
    cached = st.session_state.get(KEY_LLM_STATUS)
    if cached is None:
        st.info(configured_message)
    elif cached is status_bar.LLMConnectivity.connected:
        st.success(st.session_state.get(KEY_LLM_MESSAGE) or "Đã kết nối provider.")
    else:
        st.error(
            st.session_state.get(KEY_LLM_MESSAGE)
            or "Lần kiểm tra kết nối gần nhất thất bại."
        )
    if llm_client is None:
        st.caption("Chưa dựng được client nên không thể kiểm tra kết nối.")
        return
    if st.button("Kiểm tra kết nối", key="novel_ai_probe_llm"):
        with st.spinner("Đang gọi provider…"):
            ok, message = probe_llm(llm_client)
        st.session_state[KEY_LLM_STATUS] = (
            status_bar.LLMConnectivity.connected
            if ok
            else status_bar.LLMConnectivity.error
        )
        st.session_state[KEY_LLM_MESSAGE] = message
        st.rerun()


# ---------------------------------------------------------------------------
# Recovery và Arbiter panel
# ---------------------------------------------------------------------------


def render_recovery_banner(project: Project) -> None:
    """Banner recovery + chặn ghi khi cần recovery thủ công (architecture.md mục 7)."""
    import streamlit as st

    from novel_ai.core import storage

    if not storage.needs_recovery(project):
        return
    pending = storage.pending_operation_ids(project)
    if storage.requires_manual_recovery(project):
        st.error(
            "Project đang **read-only**: có transaction không tự recovery được "
            f"(pending: {', '.join(pending) or 'không đọc được manifest'}). "
            "Bước tiếp theo: kiểm tra thủ công `.ops/pending/` của project rồi khôi "
            "phục file bị sửa ngoài app; UI không tự ghi đè dữ liệu."
        )
        return
    st.warning(
        "Có transaction dở cần recovery trước khi ghi tiếp. "
        f"Pending: {', '.join(pending) or 'chỉ còn lock stale'}. "
        "Backend chỉ hoàn tất commit đã staged, không merge trùng."
    )
    if st.button("Chạy recovery (backend)", key="novel_ai_recover"):
        report = storage.recover_pending(project)
        for message in report.messages:
            st.info(message)
        if report.status == storage.RECOVERY_MANUAL:
            st.error(
                "Recovery cần người xử lý: target bị sửa ngoài app hoặc thiếu staged "
                "file. Xem `.ops/pending/` và `history/` của project."
            )
        else:
            st.success("Recovery hoàn tất; app đọc lại state ở lần render sau.")


def render_arbiter_panel(project: Project) -> None:
    """Panel Arbiter đứng riêng (giữ API T19): cùng nội dung với pane phải.

    Shell T25 gọi `_render_arbiter_pane` vì pane cần biết có LLM client hay không
    để disable nút điều hướng; hàm này giữ lại cho caller cũ và test.
    """
    _render_arbiter_pane(project, llm_client=None)

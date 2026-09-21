"""AppTest offline cho Streamlit shell (T19) với bố cục 3 vùng (T25).

Sáu điều được kiểm tra ở đây, tất cả offline (không server, không mạng):

1. App chạy không exception khi `projects_root` tạm còn trống.
2. Tạo project qua UI ghi `projects/<slug>/project.json` và app render lại sạch.
3. Sau khi có project, status bar + pane Arbiter hiển thị, và khi chưa có Base
   Idea thì gợi ý next step là chốt Base Idea.
4. Rerun thuần (không bấm gì) **không** ghi file: fingerprint toàn cây project
   giữ nguyên.
5. Render không phát API call: dùng `FakeLLMClient` và chứng minh không có
   `complete` nào được gọi, cũng không có kết quả probe nào bị ghi vào state.
6. Bố cục theo `novel_ai_spec_v0.2.md` mục 28: nav workspace ở đỉnh, pane
   `Project`/`Current workspace`/`Arbiter`, status bar ghim ở đáy.

Fixture trong `tests/conftest.py` đã xoá mọi biến `NOVEL_AI_*` trước mỗi test nên
máy chạy test không ảnh hưởng kết quả.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from novel_ai.core import storage
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.project import Project, slugify
from novel_ai.ui import layout

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "novel_ai" / "app.py"

CREATE_FORM = "novel_ai_create_project"
CREATE_BUTTON = f"FormSubmitter:{CREATE_FORM}-Tạo project"
TITLE = "Nồi Canh Bên Đường"


@pytest.fixture
def app_projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`projects_root` tạm cho app, set trước khi `get_config()` được gọi."""
    root = tmp_path / "projects"
    monkeypatch.setenv("NOVEL_AI_PROJECTS_ROOT", str(root))
    return root


def _all_text(*element_lists) -> str:
    return " ".join(element.value for elements in element_lists for element in elements)


def _footer_text(at: AppTest) -> str:
    """Markdown của status bar ghim ở đáy (khối mang class `dsh-shell-footer`)."""
    matches = [
        str(element.value)
        for element in at.markdown
        if 'class="dsh-shell-footer"' in str(element.value)
    ]
    return matches[-1] if matches else ""


def _button_keys(at: AppTest) -> list[str]:
    return [button.key for button in at.button]


def _fingerprint_tree(root: Path) -> dict[str, str]:
    """`relpath -> fingerprint` cho **mọi** file trong cây project (kể cả `.ops`)."""
    return {
        str(path.relative_to(root)).replace("\\", "/"): storage.file_fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _new_app(**session_state: object) -> AppTest:
    """AppTest cho entrypoint thật; `**session_state` seed UI working state."""
    at = AppTest.from_file(str(APP_PATH))
    for key, value in session_state.items():
        at.session_state[key] = value
    at.run(timeout=60)
    return at


def _workspace_module_exists(workspace: str) -> bool:
    """True nếu page module của workspace import được (T20+ nối dần).

    Dùng để test router không phụ thuộc việc T20/T21/T22 đã nối page nào tại thời
    điểm chạy.
    """
    import importlib.util

    module_name = layout.WORKSPACE_MODULES[workspace]
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _create_project(at: AppTest, title: str = TITLE) -> None:
    at.text_input(key="novel_ai_new_title").set_value(title)
    at.button(key=CREATE_BUTTON).click()
    at.run(timeout=60)


def test_app_runs_with_empty_projects_root(app_projects_root: Path) -> None:
    at = _new_app()

    assert not at.exception
    assert app_projects_root.exists() is False or list(app_projects_root.iterdir()) == []
    assert not at.button(key=CREATE_BUTTON).value
    rendered = _all_text(at.markdown, at.caption, at.info)
    assert "Chưa có project nào đang mở" in rendered
    assert "Workspace này sẽ được nối ở T20/T21/T22" not in rendered


def test_create_project_through_ui_writes_project_json_and_renders(
    app_projects_root: Path,
) -> None:
    at = _new_app()
    _create_project(at)

    assert not at.exception
    project_json = app_projects_root / slugify(TITLE) / "project.json"
    assert project_json.is_file()
    assert Project.open(app_projects_root, slugify(TITLE)).config.title == TITLE
    # Rerun không tương tác: app vẫn mở project vừa tạo và không lỗi.
    at.run(timeout=60)
    assert not at.exception
    assert (app_projects_root / slugify(TITLE) / "project.json").is_file()


def test_status_bar_and_arbiter_show_next_step_for_missing_base_idea(
    app_projects_root: Path,
) -> None:
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    captions = _all_text(at.caption)
    # Status bar ở đáy (spec mục 28) mang project id + chapter + prose revision.
    footer = _footer_text(at)
    assert footer, "phải có status bar ở đáy trang"
    assert project.config.project_id in footer
    assert "chưa có prose revision" in footer
    assert "chưa có chapter đang làm" in footer
    assert "openai compatible" in footer.lower()
    # Pane Arbiter: chưa có Base Idea -> gợi ý chốt Base Idea, và nói rõ blocking.
    arbiter_text = _all_text(at.markdown, at.caption)
    assert "Chốt Base Idea" in arbiter_text
    assert "Bước gợi ý: Chốt Base Idea" in arbiter_text
    assert "blocking" in arbiter_text
    # Trạng thái từng tầng hiển thị dạng bảng trong pane Arbiter (spec mục 29).
    assert "Base Idea — chưa có" in arbiter_text
    assert "Timeline —" in arbiter_text
    # Project id cũng hiển thị ở pane trái (PROJECT), không chỉ ở status bar.
    assert project.config.project_id in captions


def test_shell_layout_has_top_nav_three_panes_and_bottom_status_bar(
    app_projects_root: Path,
) -> None:
    """Bố cục spec mục 28: nav trên, 3 pane giữa, status bar ở đáy."""
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    # Nav workspace ở đỉnh: một radio ngang mang đủ nhãn nav, không nằm ở sidebar.
    nav = at.radio(key=layout.KEY_WORKSPACE_NAV)
    assert nav.proto.horizontal is True
    assert list(nav.options) == layout.nav_labels()
    assert [radio.key for radio in at.sidebar.radio] == []
    # Ba pane giữa trang: PROJECT | CURRENT WORKSPACE | ARBITER.
    markdown = _all_text(at.markdown)
    for title in ("Project", "Current workspace", "Arbiter"):
        assert f'class="dsh-pane-title">{title}</div>' in markdown
    # Status bar ghim ở đáy bằng chính class của shell.
    footer = _footer_text(at)
    assert "API —" in footer
    assert "OpenAI Compatible" in footer
    assert "Context:" in footer


def test_tree_pane_renders_artifact_tree_without_radio(
    app_projects_root: Path,
) -> None:
    """Pane trái vẽ cây thật (group + node), không còn radio phẳng."""
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    markdown = _all_text(at.markdown)
    assert "**Base Idea**" in markdown
    assert "**Premise**" in markdown
    assert "**Chapters**" in markdown
    # Không còn radio cây project kiểu T19 (chỉ còn nav workspace).
    assert [radio.key for radio in at.radio] == [layout.KEY_WORKSPACE_NAV]


def test_pure_rerun_does_not_write_any_file(app_projects_root: Path) -> None:
    at = _new_app()
    _create_project(at)
    project_root = app_projects_root / slugify(TITLE)
    before = _fingerprint_tree(project_root)
    assert before, "project mới phải có ít nhất project.json và co_create.json"

    at.run(timeout=60)
    at.run(timeout=60)

    assert not at.exception
    assert _fingerprint_tree(project_root) == before


def test_render_does_not_call_llm_complete(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    # Cấu hình mặc định offline: client là fake, và build không gọi mạng.
    client, error = layout.build_llm_client(layout.get_config())
    assert isinstance(client, FakeLLMClient)
    assert error is None
    assert client.calls == []
    # Không có kết quả probe nào bị ghi => render không phát request nào.
    assert layout.KEY_LLM_STATUS not in at.session_state
    assert layout.KEY_LLM_MESSAGE not in at.session_state
    assert "Đang dùng fake LLM offline" in _all_text(at.info)


def test_workspace_nav_navigates_and_keeps_project_open(app_projects_root: Path) -> None:
    """Nav workspace (đỉnh) đổi đúng workspace và giữ project đang mở.

    Hành vi thông báo phụ thuộc module page đã tồn tại hay chưa: T20–T22 đã nối
    hết 9 page, nên nhánh "chưa nối" chỉ còn kiểm được qua hằng số hợp đồng.
    """
    at = _new_app()
    _create_project(at)
    project_root = app_projects_root / slugify(TITLE)
    before = _fingerprint_tree(project_root)

    labels = layout.nav_labels()
    keys = [key for key, _label in layout.project_tree.WORKSPACES]
    # Workspace chưa có module phải nói thật là chưa nối, không giả hoạt động.
    pending = sorted(
        workspace
        for workspace in layout.WORKSPACE_MODULES
        if not _workspace_module_exists(workspace)
    )
    not_wired_checked = False
    for workspace in pending:
        at.radio(key=layout.KEY_WORKSPACE_NAV).set_value(
            layout.label_for_workspace(workspace)
        )
        at.run(timeout=60)

        assert not at.exception
        assert at.session_state[layout.KEY_WORKSPACE] == workspace
        assert f"`{workspace}`" in _all_text(at.caption)
        assert layout.WORKSPACE_NOT_WIRED_TEMPLATE in _all_text(at.info)
        not_wired_checked = True

    # Workspace đã có module phải render thật: có nội dung page, không exception,
    # và không hiện thông báo "chưa nối".
    wired = sorted(
        workspace
        for workspace in layout.WORKSPACE_MODULES
        if _workspace_module_exists(workspace)
    )
    assert wired, "phải có ít nhất một workspace đã nối để kiểm nhánh render"
    for workspace in wired:
        at.radio(key=layout.KEY_WORKSPACE_NAV).set_value(layout.label_for_workspace(workspace))
        at.run(timeout=60)

        assert not at.exception, f"workspace `{workspace}` render lỗi"
        assert at.session_state[layout.KEY_WORKSPACE] == workspace
        assert f"`{workspace}`" in _all_text(at.caption)
        assert layout.WORKSPACE_NOT_WIRED_TEMPLATE not in _all_text(at.info)
        assert _all_text(at.subheader).strip(), (
            f"workspace `{workspace}` phải render nội dung của page"
        )

    assert len(labels) == len(keys)
    if not pending:
        # Khi mọi workspace đã nối, nhánh "chưa nối" ở trên không còn gì để kiểm;
        # thông báo trung thực vẫn phải giữ nguyên hợp đồng router của T19.
        assert (
            "T20/T21/T22" in layout.WORKSPACE_NOT_WIRED_TEMPLATE
            and "chưa có hành vi nào chạy được" in layout.WORKSPACE_NOT_WIRED_TEMPLATE
        )
    assert not_wired_checked or wired
    # Điều hướng qua các workspace là render thuần: không ghi file project.
    assert _fingerprint_tree(project_root) == before


def test_open_existing_project_from_sidebar_selectbox(app_projects_root: Path) -> None:
    seeded = Project.create(app_projects_root, "Truyện Đã Có")
    at = _new_app()
    assert not at.exception

    at.selectbox(key="novel_ai_existing_project").set_value(seeded.slug)
    at.button(key="novel_ai_open_button").click()
    at.run(timeout=60)

    assert not at.exception
    assert at.session_state[layout.KEY_OPEN_PROJECT] == seeded.slug
    assert seeded.config.project_id in _all_text(at.caption)


def test_action_result_does_not_leak_between_workspaces(app_projects_root: Path) -> None:
    """Kết quả action được scope theo workspace (C1 của review findings T24).

    Trước đây `novel_ai/ui/__init__.py` dùng **một** key chung cho mọi workspace,
    nên kết quả của workspace này hiện sang workspace khác (và workspace render
    trước xóa luôn key đó). Test gieo kết quả cho từng workspace rồi kiểm mỗi
    workspace chỉ thấy kết quả của chính nó.
    """
    from novel_ai.ui import action_result_key

    project = Project.create(app_projects_root, TITLE)
    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    co_create_marker = "Kết quả riêng của Co-create"
    review_marker = "Kết quả riêng của Review"
    assert at.session_state[layout.KEY_WORKSPACE] == "co_create"

    # Gieo kết quả cho Co-create khi đang ở Review: không được rò sang Review.
    at.radio(key=layout.KEY_WORKSPACE_NAV).set_value("Review")
    at.session_state[action_result_key("co_create")] = {"message": co_create_marker}
    at.run(timeout=60)
    assert not at.exception
    assert at.session_state[layout.KEY_WORKSPACE] == "review"
    assert co_create_marker not in _all_text(at.success)

    # Ở Review thì chỉ thấy kết quả của Review.
    at.session_state[action_result_key("review")] = {"message": review_marker}
    at.run(timeout=60)
    assert not at.exception
    success = _all_text(at.success)
    assert review_marker in success
    assert co_create_marker not in success

    # Quay lại Co-create: kết quả của Co-create vẫn còn vì workspace khác không xóa.
    at.radio(key=layout.KEY_WORKSPACE_NAV).set_value("Co-create")
    at.run(timeout=60)
    assert not at.exception
    assert co_create_marker in _all_text(at.success)


def test_recovery_banner_reports_recoverable_transaction(app_projects_root: Path) -> None:
    """Transaction commit dở nhưng tự recovery được -> banner + action recovery."""
    from novel_ai.core.models import (
        OperationManifest,
        OperationStatus,
        WriteSetEntry,
    )

    project = Project.create(app_projects_root, TITLE)
    _seed_pending_operation(
        project,
        operation_id="op_recoverable",
        status=OperationStatus.committing,
        entries=[
            WriteSetEntry(
                target="idea/base_idea.md",
                staged=".ops/pending/op_recoverable/staged/idea/base_idea.md",
                before="history/op_recoverable/before/idea/base_idea.md",
                expected_old_hash="",
                staged_hash="deadbeef",
            )
        ],
    )
    pending_dir = project.paths.ops_pending_dir / "op_recoverable" / "staged" / "idea"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "base_idea.md").write_text("Base Idea\n", encoding="utf-8")

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    assert "transaction dở" in _all_text(at.warning)
    assert "novel_ai_recover" in _button_keys(at)


def test_recovery_banner_blocks_write_when_manual_recovery_required(
    app_projects_root: Path,
) -> None:
    """Manifest không đọc được -> read-only, có giải thích bước tiếp theo."""
    project = Project.create(app_projects_root, TITLE)
    pending_dir = project.paths.ops_pending_dir / "op_manual"
    pending_dir.mkdir(parents=True, exist_ok=True)
    storage.write_json_atomic(
        pending_dir / "manifest.json",
        {"operation_id": "op_manual", "status": "not-a-valid-status"},
        operation_id="op_manual.pending",
    )

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    errors = _all_text(at.error)
    assert "read-only" in errors
    assert ".ops/pending/" in errors
    assert "novel_ai_recover" not in _button_keys(at)


def _seed_pending_operation(
    project: Project,
    *,
    operation_id: str,
    status,
    entries: list,
) -> None:
    from novel_ai.core.models import OperationManifest

    pending_dir = project.paths.ops_pending_dir / operation_id
    pending_dir.mkdir(parents=True, exist_ok=True)
    manifest = OperationManifest(
        operation_id=operation_id,
        operation_type="finalize",
        status=status,
        created_at="2026-09-19T10:00:00+07:00",
        updated_at="2026-09-19T10:00:00+07:00",
        write_set=entries,
    )
    storage.write_json_atomic(
        pending_dir / "manifest.json", manifest, operation_id=f"{operation_id}.pending"
    )

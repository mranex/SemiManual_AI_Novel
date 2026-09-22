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
from novel_ai.core.models import OperationStatus
from novel_ai.core.project import Project, slugify
from novel_ai.ui import arbiter as arbiter_ui
from novel_ai.ui import layout

import t27_support

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


def _open_arbiter_panel(at: AppTest) -> None:
    """Mở panel Arbiter bằng control compact ở toolbar (T32).

    Panel mặc định đóng để workspace rộng; chi tiết (status rows + suggestion +
    nút điều hướng) chỉ render khi user mở.
    """
    at.button(key=layout.KEY_ARBITER_TOGGLE).click()
    at.run(timeout=60)


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
    # Dù panel đóng, next step + mức độ blocking vẫn phải nhìn thấy ở main (D018).
    main_text = _all_text(at.markdown, at.caption)
    assert "Bước gợi ý: Chốt Base Idea" in main_text
    assert "blocking" in main_text
    assert "1 blocker" in " ".join(button.label for button in at.button)
    # Project id hiển thị trong drawer trái (PROJECT), không chỉ ở status bar.
    assert project.config.project_id in captions

    _open_arbiter_panel(at)

    assert not at.exception
    detail_text = _all_text(at.markdown, at.caption)
    # Trạng thái từng tầng hiển thị dạng bảng trong panel Arbiter (spec mục 29).
    assert "Base Idea — chưa có" in detail_text
    assert "Timeline —" in detail_text
    assert "Chốt Base Idea" in detail_text


def test_shell_layout_has_top_nav_project_drawer_and_compact_arbiter(
    app_projects_root: Path,
) -> None:
    """D018: nav trên, Project trong sidebar, Arbiter compact, status bar ở đáy."""
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    # Nav workspace ở đỉnh: một radio ngang mang đủ nhãn nav, không nằm ở sidebar.
    nav = at.radio(key=layout.KEY_WORKSPACE_NAV)
    assert nav.proto.horizontal is True
    assert list(nav.options) == layout.nav_labels()
    assert [radio.key for radio in at.sidebar.radio] == []
    # Vai trò vẫn còn: PROJECT nằm trong drawer trái, CURRENT WORKSPACE ở main.
    sidebar_text = _all_text(at.sidebar.markdown, at.sidebar.caption)
    assert 'class="dsh-pane-title">Project</div>' in sidebar_text
    assert "**Base Idea**" in sidebar_text, "cây project phải nằm trong drawer trái"
    main_text = _all_text(at.markdown)
    assert 'class="dsh-pane-title">Current workspace</div>' in main_text
    # Panel Arbiter đóng mặc định: chỉ còn control compact, không có cột detail.
    assert 'class="dsh-pane-title">Arbiter</div>' not in main_text
    assert layout.KEY_ARBITER_TOGGLE in _button_keys(at)
    assert "Arbiter ·" in " ".join(button.label for button in at.button)
    # Status bar ghim ở đáy bằng chính class của shell.
    footer = _footer_text(at)
    assert "API —" in footer
    assert "OpenAI Compatible" in footer
    assert "Context:" in footer

    _open_arbiter_panel(at)

    assert not at.exception
    assert 'class="dsh-pane-title">Arbiter</div>' in _all_text(at.markdown)


def test_arbiter_panel_toggle_keeps_state_and_writes_nothing(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T32: mở/đóng panel không ghi project, không gọi LLM, không mất node đang chọn."""
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=2
    )
    client = FakeLLMClient()
    monkeypatch.setattr(
        layout, "build_llm_client", lambda *_args, **_kwargs: (client, None)
    )
    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    project_root = app_projects_root / project.slug
    at.selectbox(key=f"{layout.KEY_TREE_NODE}_selection").set_value("skeleton_ch_0001")
    at.run(timeout=60)
    assert at.session_state[layout.KEY_TREE_NODE] == "skeleton_ch_0001"

    before = _fingerprint_tree(project_root)
    at.button(key=layout.KEY_ARBITER_TOGGLE).click()
    at.run(timeout=60)
    assert at.session_state[layout.KEY_ARBITER_OPEN] is True
    assert "Skeleton · Ch.1 Chương 1" in " ".join(
        option for option in at.selectbox(key=f"{layout.KEY_TREE_NODE}_selection").options
    )
    at.button(key=layout.KEY_ARBITER_TOGGLE).click()
    at.run(timeout=60)

    assert at.session_state[layout.KEY_ARBITER_OPEN] is False
    assert at.session_state[layout.KEY_TREE_NODE] == "skeleton_ch_0001"
    assert _fingerprint_tree(project_root) == before
    assert client.calls == []


def test_blocking_notices_visible_when_drawer_and_arbiter_closed(
    app_projects_root: Path,
) -> None:
    """Cảnh báo recovery/read-only/stale vẫn thấy ở main dù hai panel đóng (D018)."""
    project = Project.create(app_projects_root, TITLE)
    _seed_pending_operation(
        project,
        operation_id="op_notice",
        status=OperationStatus.committing,
        entries=[],
    )

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    # Panel Arbiter đóng mặc định: detail không render, nhưng cảnh báo vẫn ở main.
    assert 'class="dsh-pane-title">Arbiter</div>' not in _all_text(at.markdown)
    warnings = _all_text(at.warning)
    assert "transaction dở" in warnings
    assert "Bước gợi ý:" in _all_text(at.caption)


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


# ---------------------------------------------------------------------------
# BUG-001 — project tree không được lồng expander (T27)
# ---------------------------------------------------------------------------


def _open_workspace(at: AppTest, workspace: str) -> None:
    at.radio(key=layout.KEY_WORKSPACE_NAV).set_value(layout.label_for_workspace(workspace))
    at.run(timeout=60)


def _tree_markdown(at: AppTest) -> str:
    return _all_text(at.markdown)


def test_project_tree_renders_one_chapter_without_nested_expander(
    app_projects_root: Path,
) -> None:
    """BUG-001: project có đúng một chapter phải render được qua entrypoint thật.

    Trước fix, nhánh `Chapters` mở expander rồi từng chapter (có Skeleton và
    Reconciliation) mở expander thứ hai, Streamlit raise
    `Expanders may not be nested inside other expanders.` và sập toàn shell.
    """
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=1
    )
    assert t27_support.chapter_ids(project) == ["ch_0001"]

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception, "project tree crash khi project có một chapter"
    markdown = _tree_markdown(at)
    assert "**Ch.1 Chương 1**" in markdown
    assert "**Skeleton**" in markdown
    assert "**Reconciliation**" in markdown


def test_project_tree_renders_many_chapters_without_nested_expander(
    app_projects_root: Path,
) -> None:
    """BUG-001 với nhiều chapter: đủ Chapter/Skeleton/Reconciliation, không exception."""
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=3
    )
    chapter_ids = t27_support.chapter_ids(project)
    assert chapter_ids == ["ch_0001", "ch_0002", "ch_0003"]

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception, "project tree crash khi project có nhiều chapter"
    markdown = _tree_markdown(at)
    for number in (1, 2, 3):
        assert f"**Ch.{number} Chương {number}**" in markdown
    assert markdown.count("**Skeleton**") == 3
    assert markdown.count("**Reconciliation**") == 3
    # Cây chỉ còn một tầng expander: nhóm `Chapters` không lồng trong nhóm khác.
    assert "Chapters (3)" in [element.label for element in at.expander]


def test_project_tree_selection_still_reaches_skeleton_and_reconciliation(
    app_projects_root: Path,
) -> None:
    """Sau fix vẫn chọn được chapter, Skeleton và Reconciliation trong pane trái."""
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=2
    )

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    selectbox = at.selectbox(key=f"{layout.KEY_TREE_NODE}_selection")
    options = list(selectbox.options)
    assert any("Ch.1 Chương 1" in option for option in options)
    assert sum("Skeleton" in option for option in options) == 2
    assert sum("Reconciliation" in option for option in options) == 2

    at.selectbox(key=f"{layout.KEY_TREE_NODE}_selection").set_value("skeleton_ch_0001")
    at.run(timeout=60)

    assert not at.exception
    assert at.session_state[layout.KEY_TREE_NODE] == "skeleton_ch_0001"
    assert "Đang xem:" in _all_text(at.caption)


def _short_plan_generate_button_key(at: AppTest) -> str:
    keys = [
        key
        for key in _button_keys(at)
        if key.startswith("FormSubmitter:novel_ai_short_plan_generate_")
    ]
    assert keys, "không thấy nút generate Short Plan trong shell"
    return keys[0]


def _fill_writing_constraints(at: AppTest) -> None:
    """Điền `pov`/`length_guidance` cho mọi chapter được giao (contract viết)."""
    filled = 0
    for widget in at.text_input:
        key = widget.key or ""
        if key.endswith("_pov"):
            widget.set_value("ngôi ba giới hạn theo Sở Dương")
            filled += 1
        elif key.endswith("_length_guidance"):
            widget.set_value("1500–2000 từ")
    assert filled, "không thấy ô nhập `pov` của chapter được giao"


def _accept_short_plan_through_shell(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch, *, chapters: int
) -> tuple[Project, AppTest, FakeLLMClient]:
    """Generate rồi Accept Short Plan bằng nút thật trong shell (BUG-001 trigger).

    `build_llm_client` bị thay bằng **một** `FakeLLMClient` scripted nên không có
    request mạng, và đếm được chính xác số lần gọi LLM; phần còn lại đi đúng đường
    thật: page → service → transaction ghi `chapter.json` → `st.rerun()` → shell
    render lại project tree có chapter.
    """
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=chapters, short_plan="none"
    )
    client = FakeLLMClient([t27_support.short_plan_response(project)])
    monkeypatch.setattr(
        layout, "build_llm_client", lambda *_args, **_kwargs: (client, None)
    )

    at = _new_app(
        **{
            layout.KEY_OPEN_PROJECT: project.slug,
            layout.KEY_WORKSPACE: "short_plan",
            layout.KEY_WORKSPACE_NAV: layout.label_for_workspace("short_plan"),
        }
    )
    assert not at.exception
    _fill_writing_constraints(at)
    at.button(key=_short_plan_generate_button_key(at)).click()
    at.run(timeout=60)
    assert not at.exception
    assert "novel_ai_short_plan_accept" in _button_keys(at), (
        "generate phải tạo candidate để bấm Accept"
    )

    at.button(key="novel_ai_short_plan_accept").click()
    at.run(timeout=60)
    return project, at, client


def test_accept_short_plan_in_full_shell_reruns_and_reopens_without_crash(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trigger thật của BUG-001: Accept Short Plan trong shell → rerun → reopen."""
    project, at, client = _accept_short_plan_through_shell(
        app_projects_root, monkeypatch, chapters=2
    )

    assert not at.exception, "shell crash ngay sau Accept Short Plan"
    chapter_ids = t27_support.chapter_ids(project)
    assert chapter_ids == ["ch_0001", "ch_0002"]
    project_root = app_projects_root / project.slug
    after_accept = _fingerprint_tree(project_root)

    # Rerun thuần sau accept: render lại cây có chapter, không ghi thêm file.
    at.run(timeout=60)
    assert not at.exception
    assert _fingerprint_tree(project_root) == after_accept

    # "Restart": AppTest mới hoàn toàn trên cùng project đã có chapter.
    reopened = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    assert not reopened.exception
    assert "**Ch.1 Chương 1**" in _tree_markdown(reopened)
    assert _fingerprint_tree(project_root) == after_accept
    # Chỉ đúng một LLM call: lần bấm generate. Rerun/render không phát request nào.
    assert len(client.calls) == 1


def test_accept_short_plan_keeps_metadata_pins_and_previous_chapter(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept tạo đúng metadata/pin/previous chapter; rerun và reopen giữ nguyên."""
    project, at, _client = _accept_short_plan_through_shell(
        app_projects_root, monkeypatch, chapters=2
    )
    assert not at.exception

    first = storage.load_chapter(project, "ch_0001")
    second = storage.load_chapter(project, "ch_0002")
    assert first is not None and second is not None
    assert first.status.value == "planned"
    assert first.previous_chapter_id is None
    assert second.previous_chapter_id == "ch_0001"
    assert first.short_plan_pin is not None
    assert first.short_plan_pin.revision == 1

    at.run(timeout=60)
    reopened = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    assert not reopened.exception
    again_first = storage.load_chapter(project, "ch_0001")
    assert again_first is not None
    assert again_first.model_dump(mode="json") == first.model_dump(mode="json")


def test_project_without_chapter_still_renders_tree(
    app_projects_root: Path,
) -> None:
    """Nhánh còn lại của BUG-001: project chưa có chapter vẫn render như trước."""
    project = Project.create(app_projects_root, TITLE)

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})

    assert not at.exception
    markdown = _tree_markdown(at)
    assert "**Chapters**" in markdown
    assert "**Skeleton**" not in markdown
    tree_expanders = [element.label for element in at.expander]
    assert "Foundation (4)" in tree_expanders
    assert "Plans (2)" in tree_expanders


# ---------------------------------------------------------------------------
# BUG-002 — nút điều hướng Arbiter (T28)
# ---------------------------------------------------------------------------


def _arbiter_button_key(index: int, code: str) -> str:
    return f"novel_ai_arbiter_open_{index}_{code}"


def _assert_navigated(at: AppTest, workspace: str, *, page_marker: str | None = None) -> None:
    """Navbar radio, workspace id và caption của shell phải khớp cùng một workspace."""
    assert at.session_state[layout.KEY_WORKSPACE] == workspace
    assert at.session_state[layout.KEY_WORKSPACE_NAV] == layout.label_for_workspace(workspace)
    assert at.radio(key=layout.KEY_WORKSPACE_NAV).value == layout.label_for_workspace(workspace)
    assert f"`{workspace}`" in _all_text(at.caption)
    if page_marker is not None:
        assert page_marker in _all_text(at.subheader)


def test_arbiter_button_switches_workspace_navbar_and_page(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-002: click nút `Chuyển tới …` thật phải đổi navbar, page và giữ nguyên state."""
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=2
    )
    report = arbiter_ui.analyze(project)
    target = report.suggestions[0]
    assert target.workspace == "skeleton", "fixture phải gợi ý sang workspace chapter"

    client = FakeLLMClient()
    monkeypatch.setattr(
        layout, "build_llm_client", lambda *_args, **_kwargs: (client, None)
    )
    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    assert at.session_state[layout.KEY_WORKSPACE] == "co_create"
    project_root = app_projects_root / project.slug
    before = _fingerprint_tree(project_root)
    _open_arbiter_panel(at)

    button = at.button(key=_arbiter_button_key(0, target.code))
    assert not button.disabled, "điều hướng chỉ để xem nên không bị chặn vì thiếu LLM"
    button.click()
    at.run(timeout=60)

    assert not at.exception, "click nút Arbiter không được raise widget-state exception"
    _assert_navigated(at, target.workspace, page_marker="Skeleton")

    # Rerun thêm hai lần: lựa chọn không bị bật lại về workspace cũ và không loop.
    at.run(timeout=60)
    at.run(timeout=60)
    assert not at.exception
    _assert_navigated(at, target.workspace)

    # Điều hướng là render thuần: không mutation project và không gọi LLM.
    assert client.calls == []
    assert _fingerprint_tree(project_root) == before


def test_arbiter_navigation_works_without_llm_client(
    app_projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Điều hướng không bị chặn khi chưa dựng được LLM client (chỉ xem workspace)."""
    project = t27_support.seed_project_with_accepted_short_plan(
        app_projects_root, chapters=2
    )
    target = arbiter_ui.analyze(project).suggestions[0]
    monkeypatch.setattr(
        layout,
        "build_llm_client",
        lambda *_args, **_kwargs: (None, "Thiếu NOVEL_AI_API_BASE_URL."),
    )

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    _open_arbiter_panel(at)
    button = at.button(key=_arbiter_button_key(0, target.code))
    assert not button.disabled
    button.click()
    at.run(timeout=60)

    assert not at.exception
    _assert_navigated(at, target.workspace)
    assert "Chưa dựng được LLM client" in _all_text(at.caption)


def test_arbiter_navigation_reaches_recovery_workspace(
    app_projects_root: Path,
) -> None:
    """Gợi ý recovery trỏ sang workspace Revision và điều hướng tới đó được."""
    project = Project.create(app_projects_root, TITLE)
    _seed_pending_operation(
        project,
        operation_id="op_nav_recovery",
        status=OperationStatus.committing,
        entries=[],
    )
    target = arbiter_ui.analyze(project).suggestions[0]
    assert target.workspace == "revision"

    at = _new_app(**{layout.KEY_OPEN_PROJECT: project.slug})
    _open_arbiter_panel(at)

    at.button(key=_arbiter_button_key(0, target.code)).click()
    at.run(timeout=60)

    assert not at.exception
    _assert_navigated(at, "revision")
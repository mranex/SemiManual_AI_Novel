"""Hợp đồng router UI và LLM client builder (T19).

Test thuần Python cho `novel_ai.ui.layout`: `build_llm_client` không raise, không
gọi mạng; `WORKSPACE_MODULES` khớp workspace id của Arbiter; và router
`render_workspace` báo thật khi page chưa tồn tại (dùng Streamlit AppTest offline
với một callback nhỏ, không mở server).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from novel_ai.config import AppConfig, load_config
from novel_ai.core.llm import FakeLLMClient, LLMError, OpenAICompatibleClient
from novel_ai.core.project import Project
from novel_ai.ui import arbiter, layout

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> AppConfig:
    """App config cô lập: projects root tạm, không đọc `.env` của máy."""
    monkeypatch.setenv("NOVEL_AI_PROJECTS_ROOT", str(tmp_path / "projects"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_config(repo_root=REPO_ROOT)


def test_build_llm_client_uses_fake_by_default_without_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(monkeypatch, tmp_path)

    client, error = layout.build_llm_client(config)

    assert error is None
    assert isinstance(client, FakeLLMClient)
    assert client.calls == []


def test_build_llm_client_force_fake_ignores_real_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(
        monkeypatch,
        tmp_path,
        NOVEL_AI_USE_FAKE_LLM="false",
        NOVEL_AI_API_BASE_URL="http://localhost:1234/v1",
        NOVEL_AI_API_KEY="secret-key",
        NOVEL_AI_MODEL="local-model",
    )

    client, error = layout.build_llm_client(config, force_fake=True)

    assert isinstance(client, FakeLLMClient)
    assert error is None


def test_build_llm_client_reports_missing_config_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(monkeypatch, tmp_path, NOVEL_AI_USE_FAKE_LLM="false")

    client, error = layout.build_llm_client(config)

    assert client is None
    assert error is not None
    # Message nêu tên biến môi trường cần đặt, không nêu giá trị secret.
    assert "NOVEL_AI_API_BASE_URL" in error
    assert "NOVEL_AI_API_KEY" in error
    assert "NOVEL_AI_MODEL" in error


def test_build_llm_client_message_never_echoes_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(
        monkeypatch,
        tmp_path,
        NOVEL_AI_USE_FAKE_LLM="false",
        NOVEL_AI_API_KEY="super-secret-value",
    )

    client, error = layout.build_llm_client(config)

    assert client is None
    assert error is not None
    assert "super-secret-value" not in error


def test_build_llm_client_constructs_adapter_without_network_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(
        monkeypatch,
        tmp_path,
        NOVEL_AI_USE_FAKE_LLM="false",
        NOVEL_AI_API_BASE_URL="http://127.0.0.1:9/v1",
        NOVEL_AI_API_KEY="secret-key",
        NOVEL_AI_MODEL="local-model",
    )

    client, error = layout.build_llm_client(config)

    assert error is None
    assert isinstance(client, OpenAICompatibleClient)
    assert client.config.model == "local-model"


def test_probe_llm_calls_provider_once_and_reports_failure() -> None:
    ok_client = FakeLLMClient(["pong"])
    ok, message = layout.probe_llm(ok_client)
    assert ok is True
    assert len(ok_client.calls) == 1
    assert message

    failing = FakeLLMClient([LLMError("provider không phản hồi")])
    ok, message = layout.probe_llm(failing)
    assert ok is False
    assert "provider không phản hồi" in message


def test_load_prompt_registry_reports_error_for_empty_prompt_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_root = tmp_path / "prompts"
    empty_root.mkdir()
    config = _config(monkeypatch, tmp_path, NOVEL_AI_PROMPT_ROOT=str(empty_root))

    registry, error = layout.load_prompt_registry(config)

    assert registry is None
    assert error is not None
    assert "manifest" in error


def test_workspace_modules_match_arbiter_workspaces() -> None:
    # Router phải phủ đúng bộ workspace id mà Arbiter gợi ý, để gợi ý luôn route được.
    assert set(layout.WORKSPACE_MODULES) == set(arbiter.WORKSPACES)
    assert len(set(layout.WORKSPACE_MODULES.values())) == len(layout.WORKSPACE_MODULES)


def test_not_wired_message_is_honest_about_missing_behaviour() -> None:
    assert "T20/T21/T22" in layout.WORKSPACE_NOT_WIRED_TEMPLATE
    assert "chưa có hành vi nào chạy được" in layout.WORKSPACE_NOT_WIRED_TEMPLATE


def test_navigate_to_workspace_syncs_both_keys_and_ignores_unknown_workspace(
    tmp_path: Path,
) -> None:
    """Helper điều hướng dùng chung (T28): đồng bộ cả hai key, bỏ qua id lạ.

    Chạy qua AppTest để có session state thật; helper không tạo widget nào nên gán
    key ở đây hợp lệ (đây chính là đường được `on_click` của nút Arbiter dùng).
    """
    script = tmp_path / "nav_probe.py"
    script.write_text(
        _NAV_PROBE_SCRIPT.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )

    at = AppTest.from_file(str(script))
    at.run(timeout=60)

    assert not at.exception
    rendered = " ".join(str(element.value) for element in at.markdown)
    # (workspace, nav) sau lần gọi hợp lệ, rồi workspace sau lần gọi id lạ.
    assert "probe=('skeleton', 'Skeleton', 'skeleton')" in rendered


_NAV_PROBE_SCRIPT = '''\
"""Script tạm cho AppTest: gọi trực tiếp helper điều hướng của T28."""
import sys

sys.path.insert(0, {repo_root!r})

import streamlit as st

from novel_ai.ui import layout

layout.navigate_to_workspace("skeleton")
after_valid = (
    st.session_state.get(layout.KEY_WORKSPACE),
    st.session_state.get(layout.KEY_WORKSPACE_NAV),
)
layout.navigate_to_workspace("khong_ton_tai")
st.markdown("probe=" + repr(after_valid + (st.session_state.get(layout.KEY_WORKSPACE),)))
'''


_ROUTER_PROBE_SCRIPT = '''\
"""Script tạm cho AppTest: gọi router của T19 với page chưa tồn tại."""
import sys

sys.path.insert(0, {repo_root!r})

from novel_ai.config import load_config
from novel_ai.core.project import Project
from novel_ai.ui import layout

config = load_config(
    env={{"NOVEL_AI_PROJECTS_ROOT": {projects_root!r}}}, repo_root={repo_root!r}
)
project = Project.create({projects_root!r}, "Truyện Thử Nghiệm")
ctx = layout.AppContext(
    app_config=config,
    project=project,
    registry=None,
    workspace={workspace!r},
    llm_client=None,
    llm_error=None,
)
layout.render_workspace(ctx)
'''


def _module_exists(module_name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def test_render_workspace_prints_not_wired_message_when_page_missing(
    tmp_path: Path,
) -> None:
    # T19 chưa có `novel_ai.pages`; nếu T20+ đã nối page thì test này không còn ý
    # nghĩa cho workspace đó nên được skip thay vì fail oan.
    if _module_exists("novel_ai.pages.skeleton"):
        pytest.skip("page skeleton đã được nối; kiểm tra này thuộc giai đoạn T19.")
    projects_root = tmp_path / "projects"
    script = tmp_path / "router_probe.py"
    script.write_text(
        _ROUTER_PROBE_SCRIPT.format(
            repo_root=str(REPO_ROOT),
            projects_root=str(projects_root),
            workspace="skeleton",
        ),
        encoding="utf-8",
    )

    at = AppTest.from_file(str(script))
    at.run(timeout=60)

    assert not at.exception
    assert any(
        layout.WORKSPACE_NOT_WIRED_TEMPLATE in element.value for element in at.info
    )
    assert (projects_root / "truyen-thu-nghiem" / "project.json").is_file()

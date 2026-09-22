"""Kiểm tra app config: đường dẫn, kiểu dữ liệu và ranh giới secret (T07)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_ai import config as config_module
from novel_ai.config import ENV_VARS, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_paths_are_repo_relative_and_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(env={})

    assert config.repo_root == REPO_ROOT
    assert config.projects_root == REPO_ROOT / "projects"
    assert config.prompt_root == REPO_ROOT / "docs" / "prompts" / "v1"


def test_fake_llm_is_the_offline_default() -> None:
    assert load_config(env={}).use_fake_llm is True


def test_default_prompt_root_has_registered_manifest() -> None:
    config = load_config(env={})
    assert (config.prompt_root / "manifest.json").is_file()
    assert config.warnings() == []


def test_relative_projects_root_resolves_against_repo_root() -> None:
    config = load_config(env={"NOVEL_AI_PROJECTS_ROOT": "custom_root"})
    assert config.projects_root == REPO_ROOT / "custom_root"


def test_absolute_projects_root_is_used_as_is(tmp_path: Path) -> None:
    config = load_config(env={"NOVEL_AI_PROJECTS_ROOT": str(tmp_path)})
    assert config.projects_root == tmp_path.resolve()


def test_generation_parameters_are_parsed() -> None:
    config = load_config(
        env={
            "NOVEL_AI_LLM_TIMEOUT_SECONDS": "30.5",
            "NOVEL_AI_LLM_TEMPERATURE": "0.2",
            "NOVEL_AI_LLM_MAX_TOKENS": "1024",
            "NOVEL_AI_LLM_NATIVE_STRUCTURED_OUTPUT": "true",
        }
    )
    assert config.llm_timeout_seconds == 30.5
    assert config.llm_temperature == 0.2
    assert config.llm_max_tokens == 1024
    assert config.llm_native_structured_output is True


def test_invalid_bool_and_number_raise_config_error_without_echoing_value() -> None:
    with pytest.raises(ConfigError) as bool_error:
        load_config(env={"NOVEL_AI_USE_FAKE_LLM": "maybe"})
    assert "NOVEL_AI_USE_FAKE_LLM" in str(bool_error.value)
    assert "maybe" not in str(bool_error.value)

    with pytest.raises(ConfigError) as int_error:
        load_config(env={"NOVEL_AI_LLM_MAX_TOKENS": "khong-phai-so"})
    assert "NOVEL_AI_LLM_MAX_TOKENS" in str(int_error.value)
    assert "khong-phai-so" not in str(int_error.value)


def test_warnings_report_missing_provider_settings_when_fake_llm_off() -> None:
    warnings = " ".join(load_config(env={"NOVEL_AI_USE_FAKE_LLM": "false"}).warnings())
    assert "NOVEL_AI_API_BASE_URL" in warnings
    assert "NOVEL_AI_API_KEY" in warnings
    assert "NOVEL_AI_MODEL" in warnings


def test_api_key_never_appears_in_summary_or_cli_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-t07-test-secret-never-print"
    monkeypatch.setenv("NOVEL_AI_API_KEY", secret)
    monkeypatch.setenv("NOVEL_AI_USE_FAKE_LLM", "false")

    config = load_config()
    assert config.api_key_configured is True
    assert secret not in repr(config.redacted_summary())

    assert config_module.main() == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert "api_key: set" in captured.out


def test_env_example_documents_exactly_the_supported_variables(repo_root: Path) -> None:
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert documented == set(ENV_VARS)


def test_env_example_has_no_real_secret(repo_root: Path) -> None:
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    api_key_lines = [
        line.strip()
        for line in example.splitlines()
        if line.strip().startswith("NOVEL_AI_API_KEY=")
    ]
    assert api_key_lines == ["NOVEL_AI_API_KEY="]


def test_dotenv_is_read_without_mutating_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` chỉ được đọc, không được ghi vào `os.environ`.

    Hồi quy: `load_dotenv` ghi thẳng vào `os.environ` nên cấu hình provider thật
    của máy rò sang mọi test gọi `load_config()` sau đó (test tưởng đang chạy
    offline nhưng lại dựng client thật), và `monkeypatch.delenv` không xoá được.
    """
    (tmp_path / ".env").write_text(
        "NOVEL_AI_USE_FAKE_LLM=false\n"
        "NOVEL_AI_API_BASE_URL=https://dotenv.example/v1\n"
        "NOVEL_AI_API_KEY=sk-from-dotenv-file\n"
        "NOVEL_AI_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    for name in (
        "NOVEL_AI_USE_FAKE_LLM",
        "NOVEL_AI_API_BASE_URL",
        "NOVEL_AI_API_KEY",
        "NOVEL_AI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config(repo_root=tmp_path)

    # Giá trị trong file vẫn được dùng...
    assert config.use_fake_llm is False
    assert config.api_base_url == "https://dotenv.example/v1"
    assert config.api_key == "sk-from-dotenv-file"
    assert config.model == "dotenv-model"
    # ...nhưng không biến nào bị đẩy vào môi trường process.
    for name in (
        "NOVEL_AI_USE_FAKE_LLM",
        "NOVEL_AI_API_BASE_URL",
        "NOVEL_AI_API_KEY",
        "NOVEL_AI_MODEL",
    ):
        assert name not in os.environ


def test_real_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cùng key: biến môi trường thật thắng `.env`."""
    (tmp_path / ".env").write_text(
        "NOVEL_AI_USE_FAKE_LLM=false\nNOVEL_AI_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVEL_AI_USE_FAKE_LLM", "true")
    monkeypatch.setenv("NOVEL_AI_MODEL", "env-model")

    config = load_config(repo_root=tmp_path)

    assert config.use_fake_llm is True
    assert config.model == "env-model"


def test_load_config_falls_back_to_offline_defaults_without_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Không có `.env` và không có biến môi trường: mặc định vẫn là fake LLM."""
    for name in list(os.environ):
        if name.startswith("NOVEL_AI_"):
            monkeypatch.delenv(name, raising=False)

    config = load_config(repo_root=tmp_path)

    assert config.use_fake_llm is True
    assert config.api_base_url is None
    assert config.api_key is None
    assert config.model is None


# ---------------------------------------------------------------------------
# Cô lập dotenv trên disk (T26)
# ---------------------------------------------------------------------------


def test_suite_never_reads_the_repo_dotenv() -> None:
    """`load_config()` không tham số phải bỏ qua `.env` thật ở repo root (T26).

    Hồi quy: bốn test cấu hình fail vì `load_config(repo_root=REPO_ROOT)` đọc
    `.env` của máy, nơi đang bật provider thật, nên `build_llm_client` dựng
    `OpenAICompatibleClient` thay vì `FakeLLMClient` mặc định offline. Fixture
    `isolate_novel_ai_env` trỏ seam dotenv về `tmp_path`, nên ở đây không có file
    nào và mặc định offline phải thắng.
    """
    config = load_config()

    assert config.use_fake_llm is True
    assert config.api_base_url is None
    assert config.api_key is None
    assert config.model is None


def test_suite_reads_the_fake_dotenv_in_tmp_path(tmp_path: Path) -> None:
    """Bằng chứng dương: dotenv giả trong `tmp_path` chính là file suite đọc.

    Giá trị dưới đây không tồn tại trong `.env` thật, nên test chỉ pass nếu
    fixture autouse đã chuyển seam dotenv sang `tmp_path/.env`.
    """
    (tmp_path / ".env").write_text(
        "NOVEL_AI_USE_FAKE_LLM=false\n"
        "NOVEL_AI_API_BASE_URL=https://tmp-dotenv.invalid/v1\n"
        "NOVEL_AI_API_KEY=sk-t26-fake-dotenv\n"
        "NOVEL_AI_MODEL=t26-fake-dotenv-model\n",
        encoding="utf-8",
    )
    config_module.get_config.cache_clear()

    config = load_config()

    assert config.use_fake_llm is False
    assert config.api_base_url == "https://tmp-dotenv.invalid/v1"
    assert config.model == "t26-fake-dotenv-model"
    # ...và vẫn không rò sang `os.environ`.
    assert "NOVEL_AI_MODEL" not in os.environ


def test_dotenv_seam_defaults_to_repo_root(real_dotenv_path) -> None:
    """Mặc định production không đổi: seam trỏ về `<repo_root>/.env`."""
    assert real_dotenv_path(REPO_ROOT) == REPO_ROOT / ".env"
    assert real_dotenv_path(Path("C:/tmp/x")) == Path("C:/tmp/x/.env")


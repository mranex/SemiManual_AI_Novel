"""Kiểm tra app config: đường dẫn, kiểu dữ liệu và ranh giới secret (T07)."""

from __future__ import annotations

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

"""Cấu hình dependency cho application; dựng client không gọi provider."""

from __future__ import annotations

from dataclasses import dataclass

from novel_ai.config import AppConfig, get_config
from novel_ai.core.llm import (
    FakeLLMClient,
    LLMClient,
    LLMError,
    OpenAICompatibleClient,
    llm_config_from_app_config,
    redact_secrets,
    validate_llm_config,
)
from novel_ai.core.prompts import PromptError, PromptRegistry


@dataclass(frozen=True)
class RuntimeDependencies:
    """Dependency nội bộ của Python application, không phải client DTO."""

    config: AppConfig
    registry: PromptRegistry | None
    registry_error: str | None
    llm_client: LLMClient | None
    llm_error: str | None


def build_llm_client(
    app_config: AppConfig, *, force_fake: bool = False
) -> tuple[LLMClient | None, str | None]:
    """Chọn Fake hoặc OpenAI-compatible client; không phát request."""
    if force_fake or app_config.use_fake_llm:
        return FakeLLMClient(), None
    llm_config = llm_config_from_app_config(app_config)
    problems = validate_llm_config(llm_config)
    if problems:
        return None, " ".join(problems)
    try:
        return OpenAICompatibleClient(llm_config), None
    except LLMError as exc:
        return None, redact_secrets(str(exc), llm_config)
    except Exception as exc:
        return None, (
            f"Không dựng được LLM client ({type(exc).__name__}); kiểm tra cấu hình LLM."
        )


def load_prompt_registry(
    app_config: AppConfig,
) -> tuple[PromptRegistry | None, str | None]:
    """Chỉ dùng prompt manifest v1 đã cấu hình; không fallback reference cũ."""
    try:
        registry = PromptRegistry.load(
            app_config.prompt_root, repo_root=app_config.repo_root
        )
    except PromptError as exc:
        return None, f"Không load được prompt registry v1: {exc}"
    except OSError as exc:
        return None, f"Không đọc được prompt manifest: {type(exc).__name__}."
    return registry, None


def bootstrap(config: AppConfig | None = None, *, force_fake: bool = False) -> RuntimeDependencies:
    """Dựng dependency một lần cho adapter; không đọc/ghi project, không gọi LLM."""
    resolved = config or get_config()
    registry, registry_error = load_prompt_registry(resolved)
    client, llm_error = build_llm_client(resolved, force_fake=force_fake)
    return RuntimeDependencies(resolved, registry, registry_error, client, llm_error)

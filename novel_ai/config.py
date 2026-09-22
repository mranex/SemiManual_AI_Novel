"""Cấu hình app cho Manual AI Novel (T07).

Hai loại cấu hình tách biệt:

- **App config** (module này): endpoint, model, API key, timeout, tham số sinh
  và data root. Đọc từ biến môi trường hoặc `.env` ở repo root. Không chứa
  secret thật; không bao giờ ghi vào project truyện, snapshot, fixture hay log.
- **Project config**: `projects/<project>/project.json`, thuộc T09. Không nhồi
  endpoint/API key vào project truyện.

Đường dẫn mặc định suy ra từ repo root (vị trí file này), không phụ thuộc
current working directory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROJECTS_ROOT = "projects"
DEFAULT_PROMPT_ROOT = "docs/prompts/v1"
DEFAULT_LLM_TIMEOUT_SECONDS = 120.0
DEFAULT_LLM_TEMPERATURE = 0.7
DEFAULT_LLM_MAX_TOKENS = 4096

ENV_USE_FAKE_LLM = "NOVEL_AI_USE_FAKE_LLM"
ENV_API_BASE_URL = "NOVEL_AI_API_BASE_URL"
ENV_API_KEY = "NOVEL_AI_API_KEY"
ENV_MODEL = "NOVEL_AI_MODEL"
ENV_LLM_TIMEOUT_SECONDS = "NOVEL_AI_LLM_TIMEOUT_SECONDS"
ENV_LLM_TEMPERATURE = "NOVEL_AI_LLM_TEMPERATURE"
ENV_LLM_MAX_TOKENS = "NOVEL_AI_LLM_MAX_TOKENS"
ENV_LLM_NATIVE_STRUCTURED_OUTPUT = "NOVEL_AI_LLM_NATIVE_STRUCTURED_OUTPUT"
ENV_PROJECTS_ROOT = "NOVEL_AI_PROJECTS_ROOT"
ENV_PROMPT_ROOT = "NOVEL_AI_PROMPT_ROOT"

#: Toàn bộ biến môi trường app đọc. Test đối chiếu danh sách này với `.env.example`
#: để mẫu cấu hình không lệch khỏi code.
ENV_VARS: tuple[str, ...] = (
    ENV_USE_FAKE_LLM,
    ENV_API_BASE_URL,
    ENV_API_KEY,
    ENV_MODEL,
    ENV_LLM_TIMEOUT_SECONDS,
    ENV_LLM_TEMPERATURE,
    ENV_LLM_MAX_TOKENS,
    ENV_LLM_NATIVE_STRUCTURED_OUTPUT,
    ENV_PROJECTS_ROOT,
    ENV_PROMPT_ROOT,
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Giá trị cấu hình sai kiểu.

    Message chỉ nêu tên biến, không echo giá trị, để log lỗi không làm lộ secret.
    """


def _read_str(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _read_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} phải là true hoặc false.")


def _read_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} phải là số.") from exc


def _read_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} phải là số nguyên.") from exc


def resolve_path(value: str, *, repo_root: Path) -> Path:
    """Resolve đường dẫn cấu hình: tuyệt đối giữ nguyên, tương đối tính từ repo root."""
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


@dataclass(frozen=True)
class AppConfig:
    """App config đã resolve. Không chứa secret dạng hiển thị được."""

    repo_root: Path
    projects_root: Path
    prompt_root: Path
    use_fake_llm: bool = True
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_native_structured_output: bool = False

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    def redacted_summary(self) -> dict[str, object]:
        """Bản tóm tắt an toàn để in ra CLI/UI/log: API key chỉ còn set/not set."""
        return {
            "repo_root": str(self.repo_root),
            "projects_root": str(self.projects_root),
            "projects_root_exists": self.projects_root.is_dir(),
            "prompt_root": str(self.prompt_root),
            "prompt_root_exists": self.prompt_root.is_dir(),
            "use_fake_llm": self.use_fake_llm,
            "api_base_url": self.api_base_url or "not set",
            "api_key": "set" if self.api_key_configured else "not set",
            "model": self.model or "not set",
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_temperature": self.llm_temperature,
            "llm_max_tokens": self.llm_max_tokens,
            "llm_native_structured_output": self.llm_native_structured_output,
        }

    def warnings(self) -> list[str]:
        """Cảnh báo cấu hình thiếu, không chặn khởi động và không nêu giá trị secret."""
        messages: list[str] = []
        if not self.prompt_root.is_dir():
            messages.append(
                f"Không thấy prompt root: {self.prompt_root} (kiểm tra {ENV_PROMPT_ROOT})."
            )
        elif not (self.prompt_root / "manifest.json").is_file():
            messages.append(
                f"Prompt root {self.prompt_root} thiếu manifest.json; runtime chỉ load prompt đã đăng ký."
            )
        if not self.use_fake_llm:
            if not self.api_base_url:
                messages.append(f"Đang tắt fake LLM nhưng thiếu {ENV_API_BASE_URL}.")
            if not self.api_key_configured:
                messages.append(f"Đang tắt fake LLM nhưng thiếu {ENV_API_KEY}.")
            if not self.model:
                messages.append(f"Đang tắt fake LLM nhưng thiếu {ENV_MODEL}.")
        return messages


def _dotenv_path(root: Path) -> Path:
    """File dotenv đọc khi `load_config(env=None)`; mặc định là `<root>/.env`.

    Tách thành một hàm nhỏ để test cô lập được dotenv của máy (T26): suite test
    monkeypatch seam này về thư mục tạm nên `.env` thật ở repo root không bao giờ
    ảnh hưởng kết quả. App chạy thật không đổi hành vi.
    """
    return root / ".env"


def _load_env_sources(root: Path, environ: Mapping[str, str]) -> Mapping[str, str]:
    """Ghép `.env` ở repo root với biến môi trường thật; biến thật luôn thắng.

    Đọc `.env` bằng `dotenv_values` (không mutate `os.environ`). Nhờ vậy gọi
    `load_config()` không làm rò cấu hình provider (key thật, base URL) vào
    process: test khác hay code khác đọc `os.environ` vẫn thấy môi trường sạch.
    `override=False` của `load_dotenv` trước đây ghi thẳng vào `os.environ` nên
    biến từ `.env` sống sót qua cả `monkeypatch.delenv`.
    """
    file_values = dotenv_values(_dotenv_path(root))
    merged: dict[str, str] = {
        str(name): str(value) for name, value in file_values.items() if value is not None
    }
    merged.update({str(name): str(value) for name, value in environ.items()})
    return merged


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> AppConfig:
    """Đọc app config.

    `env=None` (mặc định khi chạy app): ghép `.env` ở repo root với `os.environ`,
    trong đó biến môi trường thật luôn thắng file `.env`. Việc đọc `.env` **không**
    sửa `os.environ`.
    Truyền `env={...}` để test độc lập hoàn toàn với môi trường và `.env`.
    """
    root = (Path(repo_root) if repo_root is not None else REPO_ROOT).resolve()

    if env is None:
        source: Mapping[str, str] = _load_env_sources(root, os.environ)
    else:
        source = env

    projects_root_value = _read_str(source, ENV_PROJECTS_ROOT, DEFAULT_PROJECTS_ROOT)
    prompt_root_value = _read_str(source, ENV_PROMPT_ROOT, DEFAULT_PROMPT_ROOT)

    return AppConfig(
        repo_root=root,
        projects_root=resolve_path(projects_root_value or DEFAULT_PROJECTS_ROOT, repo_root=root),
        prompt_root=resolve_path(prompt_root_value or DEFAULT_PROMPT_ROOT, repo_root=root),
        use_fake_llm=_read_bool(source, ENV_USE_FAKE_LLM, True),
        api_base_url=_read_str(source, ENV_API_BASE_URL),
        api_key=_read_str(source, ENV_API_KEY),
        model=_read_str(source, ENV_MODEL),
        llm_timeout_seconds=_read_float(
            source, ENV_LLM_TIMEOUT_SECONDS, DEFAULT_LLM_TIMEOUT_SECONDS
        ),
        llm_temperature=_read_float(source, ENV_LLM_TEMPERATURE, DEFAULT_LLM_TEMPERATURE),
        llm_max_tokens=_read_int(source, ENV_LLM_MAX_TOKENS, DEFAULT_LLM_MAX_TOKENS),
        llm_native_structured_output=_read_bool(
            source, ENV_LLM_NATIVE_STRUCTURED_OUTPUT, False
        ),
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """App config cho UI. Cache 1 lần; sửa `.env` cần khởi động lại app."""
    return load_config()


def main() -> int:
    """In cấu hình đang dùng mà không lộ secret. Dùng cho `python -m novel_ai.config`."""
    config = load_config()
    for key, value in config.redacted_summary().items():
        print(f"{key}: {value}")
    for message in config.warnings():
        print(f"warning: {message}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint CLI
    raise SystemExit(main())

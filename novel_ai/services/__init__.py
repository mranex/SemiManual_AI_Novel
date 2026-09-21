"""Service layer của Manual AI Novel (T13–T18).

Quy ước chung cho mọi service (đọc thêm `docs/design/architecture.md`):

- Service là hàm Python thuần, **không** phụ thuộc Streamlit. UI gọi service,
  không tự đọc/ghi file và không tự chạy luật lifecycle.
- Mỗi action là một hàm riêng, làm đúng một việc, nhận `project: Project` và
  các tham số ID rõ ràng. Không service nào tự gọi service khác để "chạy tiếp
  workflow" (vi phạm luật không-agent).
- Mọi write action đi qua `novel_ai.core.storage` (atomic write + operation
  manifest) và `novel_ai.core.lifecycle` (candidate/accept/stale). Service
  không tự `Path.write_text`.
- Service **không** nhận secret LLM: nó nhận `LLMClient` đã cấu hình. App
  config/API key không bao giờ được ghi vào project.
- Guard nằm ở service (D011). Nếu guard không đạt, service raise `GuardError`
  và **không** gọi LLM.
- Output LLM luôn được validate trước khi lưu candidate/accepted; raw output
  được lưu lại để người dùng xử lý khi schema sai.
- Tham số `now: str | None = None` (ISO 8601) cho phép test cố định thời gian;
  mặc định dùng `novel_ai.core.models.now_iso()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novel_ai.core.models import ValidationResult

__all__ = [
    "ActionResult",
    "GuardError",
    "LLMUnavailableError",
    "ServiceError",
    "StaleDependencyError",
    "ValidationFailure",
]


@dataclass
class ActionResult:
    """Kết quả một action service, đủ để UI hiển thị mà không cần đọc lại file."""

    operation_id: str | None = None
    artifact_id: str | None = None
    chapter_id: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult | None = None


class ServiceError(RuntimeError):
    """Lỗi service có mã ổn định để UI/log phân loại mà không parse message."""

    code = "service_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})


class GuardError(ServiceError):
    """Action bị chặn bởi lifecycle/dependency guard. Không được gọi LLM."""

    code = "guard_failed"


class StaleDependencyError(GuardError):
    """Dependency đang stale hoặc revision đã đổi; cần user review/regenerate."""

    code = "stale_dependency"


class ValidationFailure(ServiceError):
    """Output/candidate không qua validation. Accepted state không đổi."""

    code = "validation_failed"

    def __init__(
        self,
        message: str,
        *,
        result: ValidationResult | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.result = result


class LLMUnavailableError(ServiceError):
    """Provider lỗi/timeout/cấu hình thiếu. Raw output (nếu có) đã được lưu."""

    code = "llm_unavailable"

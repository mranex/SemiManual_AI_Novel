"""Manual AI Novel — package gốc.

T07 mới khởi tạo phần tối thiểu: cấu hình app (`novel_ai.config`), entrypoint
Streamlit (`novel_ai.app`) và interface LLM + fake client offline
(`novel_ai.core.llm`).

Các module còn lại theo `docs/design/storage.md` mục 1 (`core/models.py`,
`core/validation.py`, `core/storage.py`, `core/lifecycle.py`, `core/prompts.py`,
`core/context.py`, `services/`, `ui/`, `pages/`) thuộc T08–T22 và chưa tồn tại.
Không tạo file rỗng để tính là đã hoàn thành.
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = ["__version__"]

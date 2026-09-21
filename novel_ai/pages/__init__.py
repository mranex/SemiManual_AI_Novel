"""Workspace page của Manual AI Novel (T20–T22).

Quy ước của package này (đọc thêm `novel_ai/ui/__init__.py` và
`docs/design/architecture.md` mục 1, 4):

- Mỗi module là **một workspace** và tên module phải khớp
  `novel_ai.ui.layout.WORKSPACE_MODULES`.
- Mỗi module phải có `def render(ctx: AppContext) -> None`; router của T19 gọi
  đúng hàm này, không truyền thêm tham số.
- Page chỉ **hiển thị + nhập liệu**. Mọi mutation gọi service trong
  `novel_ai.services`; page không tự đọc/ghi file project (đọc state qua
  `novel_ai.core.storage` chỉ để hiển thị) và không tự chạy luật lifecycle.
- Mọi mutation nằm sau `st.button(...)`/`st.form_submit_button(...)` trong nhánh
  `if submitted:`. Rerun thuần (không bấm gì) không sinh API call và không ghi
  file.
- Lỗi service (`GuardError`, `ValidationFailure`, `LLMUnavailableError`) được
  hiển thị theo field qua `_common.render_service_error` kèm bước tiếp theo; UI
  **không** tự sửa state để che lỗi.
- Kết quả action được đẩy qua `novel_ai.ui.set_action_result` rồi
  `novel_ai.ui.show_action_result()` ở đầu page để không lặp thông báo qua rerun.
  Kết quả được scope theo workspace trong `st.session_state` (C1 của
  `docs/design/review-findings-t24.md`), nên thông báo không rò sang workspace khác.

Helper thuần (dựng input, chọn bản ghi để hiển thị) nằm ở `_common.py` để test
được mà không cần Streamlit; logic nặng vẫn thuộc service tương ứng.
"""

from __future__ import annotations

__all__: list[str] = []

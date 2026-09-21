"""UI layer của Manual AI Novel (T19–T22).

Quy ước chung cho mọi page/component (đọc thêm `docs/design/architecture.md`):

- UI là lớp hiển thị + nhập liệu. **Mọi mutation đi qua service** (`novel_ai.services`);
  UI không tự đọc/ghi file project, không tự chạy luật lifecycle, không tự gọi LLM.
- UI **không** phải guard duy nhất: nút bị ẩn chỉ là tiện dụng, backend vẫn phải
  từ chối action sai (D011).
- Rerun/rerender không được tự sinh API call hoặc commit lần nữa. Mọi action ghi
  dữ liệu phải nằm sau `st.button(...)`/`st.form_submit_button(...)` trong nhánh
  `if submitted:`, và service đã có `operation_id` để retry an toàn.
- State bền nằm ở file project. `st.session_state` chỉ giữ UI working state
  (project đang mở, chapter đang chọn, draft đang sửa, cờ hiển thị).
- Hiển thị rõ: project/chapter/revision hiện tại, và trạng thái
  `draft` / `candidate` / `accepted` / `stale` / `partial` / `finalizing`.
- Thông báo lỗi phải nói bước tiếp theo bằng ngôn ngữ người dùng; không bắt
  người dùng sửa file JSON nội bộ cho workflow thường gặp.
- Không thêm auth/multi-user/rich-text editor/IDE.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

__all__ = [
    "ACTION_RESULT_KEY_TEMPLATE",
    "action_result_key",
    "clear_action_result",
    "current_workspace",
    "get_action_result",
    "page_header",
    "set_action_result",
    "show_action_result",
]

#: Khóa session_state lưu kết quả action gần nhất để render sau khi rerun.
#:
#: Kết quả được scope theo **workspace** (C1 của `review-findings-t24.md`): một
#: khóa dùng chung cho mọi workspace khiến thông báo của Writer hiện trong
#: workspace Review (page render trước sẽ đọc rồi xóa key), và quay lại Writer thì
#: không còn thấy kết quả của chính action vừa bấm.
ACTION_RESULT_KEY_TEMPLATE = "novel_ai_action_result_{workspace}"

#: `st.session_state` key giữ workspace đang chọn (khớp `layout.KEY_WORKSPACE`).
WORKSPACE_STATE_KEY = "novel_ai_workspace"


def current_workspace() -> str:
    """Workspace đang render theo `st.session_state` (rỗng nếu chưa chọn)."""
    return str(st.session_state.get(WORKSPACE_STATE_KEY) or "")


def action_result_key(workspace: str | None = None) -> str:
    """Khóa kết quả action của một workspace (`None` ⇒ workspace đang render)."""
    resolved = workspace if workspace is not None else current_workspace()
    return ACTION_RESULT_KEY_TEMPLATE.format(workspace=resolved)


def set_action_result(result: Any) -> None:
    """Lưu `ActionResult` (hoặc dict) của action vừa chạy, theo workspace hiện tại."""
    st.session_state[action_result_key()] = result


def get_action_result() -> Any | None:
    return st.session_state.get(action_result_key())


def clear_action_result() -> None:
    st.session_state.pop(action_result_key(), None)


def page_header(title: str, subtitle: str = "") -> None:
    """Header thống nhất cho mọi workspace: tiêu đề + project/chapter đang mở."""
    st.subheader(title)
    if subtitle:
        st.caption(subtitle)


def show_action_result() -> None:
    """Hiển thị kết quả action gần nhất **của workspace này** rồi xóa khỏi state.

    Gọi ở đầu mỗi page để kết quả của lần bấm trước vẫn thấy được nhưng không bị
    lặp lại ở các rerun sau, và không rò sang workspace khác.
    """
    result = get_action_result()
    if result is None:
        return
    clear_action_result()
    message = getattr(result, "message", None)
    if message is None and isinstance(result, dict):
        message = result.get("message")
    if message:
        st.success(str(message))
    warnings = getattr(result, "warnings", None)
    if warnings is None and isinstance(result, dict):
        warnings = result.get("warnings")
    for warning in warnings or []:
        st.warning(str(warning))

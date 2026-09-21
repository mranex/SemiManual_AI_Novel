"""Entrypoint Streamlit của Manual AI Novel (T19).

File này cố tình **mỏng**: toàn bộ shell (sidebar chọn project/workspace, status
bar, project tree, panel Arbiter, banner recovery và router workspace) nằm ở
`novel_ai.ui.layout`. Entrypoint chỉ giữ hai hàm ổn định để test import được:

- `main()` — chạy app, tương đương `layout.run()`;
- `render()` — hook render cho harness/test cũ; cũng gọi `layout.run()` và không
  còn scaffold T07 (scaffold đã được thay bằng shell thật).

Chạy: `python -m streamlit run novel_ai\\app.py`
"""

from __future__ import annotations

from novel_ai.ui import layout

__all__ = ["main", "render"]


def render() -> None:
    """Render toàn bộ app (Streamlit). Không tham số; app config đọc từ `.env`/env."""
    layout.run()


def main() -> None:
    """Entrypoint chạy app: `layout.run()`."""
    layout.run()


if __name__ == "__main__":
    main()

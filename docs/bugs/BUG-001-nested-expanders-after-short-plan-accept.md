# BUG-001 — Crash vì nested expander sau khi Accept Short Plan

## Metadata

| Thuộc tính | Giá trị |
|---|---|
| Trạng thái | Confirmed / chưa sửa |
| Mức độ | Blocker |
| Ưu tiên đề xuất | P0 |
| Phát hiện | 2026-09-22 |
| Project tái hiện | `projects/acc` |
| Thành phần | `novel_ai/ui/project_tree.py`, shell Streamlit |
| Trigger người dùng | Accept candidate Short Plan |
| Dữ liệu có bị hỏng không | Chưa thấy; accepted data parse hợp lệ, không có pending transaction |

## Mô tả

Sau khi người dùng bấm **Accept Short Plan**, backend accept plan và tạo `ChapterMetadata` cho các chapter. Page gọi `st.rerun()`. Ở lần render tiếp theo, project tree có thêm nhánh chapter và Streamlit ném exception:

```text
streamlit.errors.StreamlitAPIException: Expanders may not be nested inside other expanders.
```

Toàn app dừng render. Khởi động lại app vẫn lỗi khi mở lại project vì chapter metadata đã được lưu bền trên disk.

## Cách tái hiện đã xác nhận

### Trên project `ACC`

1. Mở project `acc` đã có Short Plan accepted revision 1.
2. Project chứa ba chapter `ch_0001`, `ch_0002`, `ch_0003`, đều status `planned`.
3. Chạy app hoặc AppTest qua `novel_ai/app.py`, seed session state `novel_ai_open_project = "acc"`.
4. App crash trong project pane trước khi workspace có thể tiếp tục dùng.

### Probe tối thiểu, không qua API

```python
from streamlit.testing.v1 import AppTest

code = """
from novel_ai.core.project import Project
from novel_ai.ui import project_tree
p = Project.open(r"C:\\Games\\Manual_AI_Novel\\projects", "acc")
project_tree.render(p)
"""
at = AppTest.from_string(code)
at.run(timeout=60)
assert at.exception
```

Kết quả thực tế: một `StreamlitAPIException` với message `Expanders may not be nested inside other expanders.`

## Kết quả mong đợi

- Accept Short Plan thành công.
- Chapter metadata xuất hiện trong project tree.
- Shell rerun bình thường và người dùng tiếp tục sang Skeleton.
- Đóng/mở lại app vẫn truy cập được project.

## Kết quả thực tế

- Accept đã commit thành công.
- Rerun toàn shell crash tại project tree.
- Project tiếp tục crash ở các lần mở sau.
- Không có đường phục hồi trong UI cho chính project đó.

## Root cause

### 1. Accept Short Plan tạo chapter hợp lệ

`novel_ai/services/short_planner.py:606`–`623` dựng `ChapterMetadata` và đưa cả Short Plan accepted lẫn các `chapter.json` vào cùng transaction. Đây là hành vi dự kiến, không phải lỗi.

`novel_ai/pages/short_plan.py:373`–`389` gọi service accept rồi `st.rerun()`. Rerun làm lỗi lộ ra ngay sau thao tác của user.

### 2. Cây project có hai tầng branch

`novel_ai/ui/project_tree.py:187`–`218` dựng cấu trúc:

```text
Chapters (group)
└── Chapter (chapter node có children)
    ├── Skeleton
    └── Reconciliation
```

Đây là cấu trúc dữ liệu hợp lý.

### 3. Renderer biến mọi branch thành expander

`novel_ai/ui/project_tree.py:321`–`329`:

```python
def _render_group(node, selected):
    ...
    with st.expander(...):
        for child in node.children:
            if child.children:
                _render_group(child, selected)
```

Hàm không chỉ render node `kind == "group"`; nó đệ quy với **bất kỳ child nào có children**. Vì vậy:

1. `Chapters` mở một expander.
2. Bên trong expander đó, `ch_0001` có children nên `_render_group(ch_0001, ...)` mở expander thứ hai.
3. Streamlit 1.41.1 cấm expander lồng expander và raise exception.

`novel_ai/ui/project_tree.py:355`–`357` là entry vào chuỗi đệ quy này từ mọi top-level node có children.

### 4. Đây là vi phạm trực tiếp constraint của dependency

Streamlit 1.41.1 được pin trong `pyproject.toml`. Guard nội bộ tại `.venv/Lib/site-packages/streamlit/delta_generator.py:593`–`596` chủ động raise khi block `expandable` có ancestor cùng loại. Đây không phải lỗi ngẫu nhiên theo browser/theme.

## Vì sao restart vẫn crash?

Short Plan accept đã commit bình thường trước khi UI rerun. Khi mở lại project:

1. `storage.list_chapter_ids(project)` vẫn trả ba chapter.
2. `build_project_tree()` dựng lại chapter nodes và hai child artifact cho từng chapter.
3. Renderer lặp lại expander lồng nhau.

Do đó restart chỉ tạo một render mới của cùng persisted state. Xóa chapter hoặc sửa tay Short Plan không phải workaround an toàn và có thể phá dependency/canon.

## Đánh giá dữ liệu `ACC`

Kiểm tra read-only cho thấy:

- Short Plan: `accepted`, revision 1.
- Chapter: `ch_0001`, `ch_0002`, `ch_0003`.
- Status cả ba: `planned`.
- `short_plan_pin.revision` cả ba: 1.
- Liên kết trước: `None → ch_0001 → ch_0002` đúng thứ tự.
- Pending operations: rỗng.

Kết luận: không có bằng chứng project bị corruption. Lỗi nằm ở presentation/render path.

## Blast radius

- Tất cả project có ít nhất một chapter metadata, không riêng `ACC`.
- Có thể xảy ra sau manual Accept hoặc auto-accept structured nếu đường đó tạo chapter metadata và shell rerun.
- Không phụ thuộc nội dung prose, số lượng chapter, provider hay response API.
- Chặn toàn bộ workspace vì project pane được render trước current workspace trong `layout.run()`.

## Tại sao test không bắt được?

### Shell test chỉ dùng project chưa có chapter

`tests/integration/test_app_entrypoint.py:185`–`199` kiểm tra cây thật nhưng tạo project mới trống. `Chapters` không có child, nên không tạo cặp expander lồng nhau.

### Test flow Short Plan bỏ qua shell

Harness tại `tests/integration/test_foundation_planning_ui.py:317`–`352` gọi trực tiếp `layout.render_workspace(ctx)`. Nó không gọi `layout.run()` và không render `_render_project_pane()`.

Flow tại `tests/integration/test_foundation_planning_ui.py:692`–`715` thực sự generate/accept Short Plan và assert không exception, nhưng chỉ trong page harness trên. Vì vậy nó xác nhận service/page riêng lẻ, không xác nhận lần rerun của app thật.

## Hướng sửa đề xuất cho turn sau

Chưa triển khai trong phiên này. Constraint cho fix:

1. Chỉ dùng tối đa một tầng `st.expander`.
2. Giữ được hierarchy dễ đọc giữa nhóm Chapters, từng Chapter, Skeleton và Reconciliation.
3. Không đổi contract dữ liệu hoặc hành vi accept Short Plan.
4. Không xóa/ẩn chapter để tránh render.
5. Không catch rồi nuốt `StreamlitAPIException`; phải tạo layout hợp lệ.

Hướng ít rủi ro nhất để cân nhắc: giữ expander cho top-level group và render chapter bên trong bằng heading/container/indentation không phải expander, hoặc chỉ dùng expander cho chapter và render nhãn `Chapters` bằng container thường. Chọn một convention nhất quán cho Foundation/Plans/Chapters.

## Regression test bắt buộc khi sửa

1. AppTest qua **entrypoint thật** `novel_ai/app.py`, không chỉ page harness.
2. Project có Short Plan accepted và một chapter: `assert not at.exception`.
3. Project có nhiều chapter: `assert not at.exception`; thấy đủ Chapter/Skeleton/Reconciliation.
4. Click Accept Short Plan trong full shell rồi kiểm tra rerun không exception.
5. Tạo AppTest mới để mô phỏng restart/reopen cùng project và kiểm tra không exception.
6. Pure rerun vẫn không ghi file và không gọi LLM.

## Acceptance criteria cho bugfix

- Không còn expander lồng nhau ở bất kỳ độ sâu nào của project tree.
- `ACC` mở được qua entrypoint thật.
- Accept Short Plan không crash và người dùng sang được Skeleton.
- Reopen/restart không crash.
- Accepted Short Plan và chapter metadata giữ nguyên.
- Regression test full-shell bao phủ project có chapter.

## Trạng thái xử lý trong phiên review

**Chưa sửa theo yêu cầu người dùng.** Chỉ tạo báo cáo và bằng chứng tái hiện.

## Resolution — T27 (2026-09-22)

**Đã sửa.** `novel_ai/ui/project_tree.py` render **một tầng** expander: `_render_group(node, selected, *, nested=False)`
chỉ mở expander cho top-level group, node con dùng `_branch_line`/indentation (`subtree_contains`,
`_BRANCH_PREFIX`); selectbox chọn node dùng nhãn riêng có hậu tố chapter và bỏ `index=None` động.

Bằng chứng (đợt nghiệm thu T40, 2026-09-22):

- `tests/integration/test_app_entrypoint.py::test_accept_short_plan_in_full_shell_reruns_and_reopens_without_crash`
  — trigger gốc: Accept Short Plan trong entrypoint thật → rerun → reopen, không `StreamlitAPIException`,
  fingerprint project không đổi sau rerun.
- `::test_project_tree_renders_one_chapter_without_nested_expander`,
  `::test_project_tree_renders_many_chapters_without_nested_expander`,
  `::test_project_tree_selection_still_reaches_skeleton_and_reconciliation`.
- `.\.venv\Scripts\python.exe -m pytest -p no:randomly` → 658 passed, 1 skipped, 0 failed.

Chi tiết bàn giao: [docs/tasks/T27-project-tree-crash.md](../tasks/T27-project-tree-crash.md);
coverage matrix: [docs/design/fix-acceptance-report.md](../design/fix-acceptance-report.md).

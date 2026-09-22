# UI-FIX-01 — Project drawer trái có thể ẩn

Trạng thái: **đề xuất sau review — chưa implement**  
Ưu tiên: **P1 / High**

## Vấn đề

Project controls đang nằm trong sidebar (`novel_ai/ui/layout.py:426-430`), nhưng project tree lại nằm trong cột main cố định (`layout.py:448-452`). Người dùng phải nhìn hai vùng Project khác nhau và không thể thu hồi chiều ngang của tree.

`project_tree.render()` đã là hàm read-only, giữ selection trong session state và không gọi LLM (`novel_ai/ui/project_tree.py:334-380`), nên có thể chuyển vào sidebar mà không đổi domain behavior.

## UX đích

- Project luôn ở bên trái.
- Dùng nút collapse sidebar mặc định của Streamlit để ẩn/hiện toàn bộ Project drawer.
- Khi mở drawer, thứ tự nội dung:
  1. tên project + project ID;
  2. Create/Open project trong vùng thu gọn;
  3. project tree;
  4. node đang xem;
  5. kết nối LLM trong expander “Kết nối”.
- Khi drawer đóng, workspace dùng toàn bộ chiều ngang còn lại.
- Đóng/mở drawer không làm mất workspace đang chọn, node đang chọn hoặc text đang sửa.

## Thiết kế đề xuất

1. Chuyển `_render_project_pane(project)` vào `with st.sidebar:`.
2. Gộp `select_project`, project metadata và `project_tree.render` dưới một header `PROJECT`.
3. Đặt create/open và LLM probe trong expander để tree là nội dung chính khi drawer mở.
4. Bỏ `project_pane` khỏi `st.columns`; main shell chỉ render workspace và control Arbiter compact.
5. Giữ nguyên `KEY_OPEN_PROJECT`, `KEY_TREE_NODE`, `KEY_WORKSPACE` và `KEY_WORKSPACE_NAV` để không phá session behavior/test hiện có.
6. Không thêm CSS để che sidebar toggle; native toggle là cơ chế collapse chính thức.

## File dự kiến tác động khi implement

- `novel_ai/ui/layout.py`
- `novel_ai/ui/project_tree.py` — chỉ nếu cần compact spacing/label, không đổi builder state
- `tests/integration/test_app_entrypoint.py`
- `tests/unit/test_layout_router.py`
- `README.md`, `docs/user-guide.md`
- contract/spec UI nếu quyết định chấp thuận layout mới

## Acceptance criteria

- [ ] Project tree không còn là cột cố định trong main canvas.
- [ ] User có thể đóng/mở drawer bằng control sidebar của Streamlit.
- [ ] Workspace rộng ra thật sự khi drawer đóng; không còn khoảng trống của cột Project.
- [ ] Create/Open, tree selection và LLM probe vẫn hoạt động.
- [ ] Pure rerun và thao tác collapse không ghi file, không gọi LLM.
- [ ] Node selection vẫn sống qua rerun và không đổi canon.
- [ ] Recovery/read-only vẫn hiện ở main workspace dù drawer đang đóng.

## Test đề xuất

- AppTest: mở project, chọn node, rerun và xác nhận selection còn nguyên.
- AppTest: main shell không còn render `PROJECT` như một column sibling của workspace.
- Fingerprint project tree trước/sau collapse/rerun phải giống nhau.
- Manual screenshot ở 1366×768 và 1920×1080, cả hai trạng thái sidebar mở/đóng.

## Ngoài phạm vi

- Không làm file explorer kiểu IDE.
- Không thêm drag/drop, rename file hoặc sửa project file trực tiếp.
- Không thay đổi project/storage contract.

## Follow-up — T32 (2026-09-22)

**Đã implement.** `novel_ai/ui/layout.py` render Project trong `with st.sidebar:` (drawer native của
Streamlit) qua `_render_project_drawer(project)`, bỏ ba cột luôn mở; workspace chiếm full width khi
Arbiter đóng; cảnh báo recovery/read-only/stale/pending vẫn hiện ở main qua
`_render_blocking_notices(project)`.

Bằng chứng:

- `tests/integration/test_app_entrypoint.py::test_shell_layout_has_top_nav_project_drawer_and_compact_arbiter`,
  `::test_blocking_notices_visible_when_drawer_and_arbiter_closed`,
  `::test_arbiter_panel_toggle_keeps_state_and_writes_nothing`.
- Trạng thái: đạt bằng AppTest; **chưa** có ảnh/visual theo 3 kích thước (mục 6 của
  [fix-acceptance-report](../design/fix-acceptance-report.md)).
- Bàn giao: [T32](../tasks/T32-workspace-shell.md).


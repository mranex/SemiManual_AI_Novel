# UI-FIX-04 — Arbiter compact và có thể thu gọn

Trạng thái: **đề xuất sau review — chưa implement**  
Ưu tiên: **P1 / High**

## Vấn đề

Arbiter hiện là cột cố định (`novel_ai/ui/layout.py:448-475`). `_render_arbiter_pane()` render:

- summary;
- recovery/read-only/stale/rolling notices;
- toàn bộ status rows;
- toàn bộ suggestion;
- một nút điều hướng cho mỗi suggestion (`layout.py:361-405`).

Khi project còn thiếu nhiều artifact, panel vừa rộng vừa cao. Điều này không phản ánh đúng vai trò sản phẩm: Arbiter chỉ gợi ý bước tiếp theo, không phải workspace chính.

## UX đích

- Mặc định chỉ hiện control compact ở góc trên bên phải, ví dụ:

```text
[Arbiter · 2 blockers · 1 stale]
```

- Bấm control mở panel/popover chứa chi tiết hiện tại.
- Đóng panel trả lại toàn bộ chiều ngang cho workspace.
- Trong trạng thái bình thường, chỉ nhấn mạnh **next step đầu tiên**; các suggestion khác nằm trong danh sách phụ.
- Recovery/read-only/blocking stale vẫn có banner ngắn trong main workspace dù panel đóng.

## Thiết kế đề xuất

### Phương án khuyến nghị

Dùng `st.popover` cho Arbiter detail và một summary badge/control ở toolbar. Lý do:

- khi đóng chỉ chiếm bề rộng của một nút;
- không cần cột rỗng;
- phù hợp Streamlit 1.41.1;
- logic `arbiter.analyze()` và `status_rows()` giữ nguyên.

Nếu AppTest hoặc accessibility của popover không đủ, fallback là toggle session state:

- collapsed: không tạo `arbiter_pane` trong `st.columns`;
- expanded: render workspace + Arbiter theo tỉ lệ rộng hơn, ví dụ `3.5 : 1`;
- nút thu gọn nằm trong toolbar và panel.

Không dùng `st.expander` bên trong cột cố định làm fix duy nhất, vì expander chỉ giảm chiều cao; chiều rộng bị chiếm vẫn còn.

## Mức thông tin

### Compact summary

- next step đầu tiên;
- số blocker;
- số stale;
- recovery/read-only badge;
- rolling reminder badge nếu đến mốc.

### Expanded detail

- trạng thái theo authority;
- active chapter;
- timeline/relationship sync;
- suggestion đã sắp priority;
- nút điều hướng tới workspace.

Auto Accept note không nên đứng trên suggestion blocking thật; có thể chuyển thành info badge/config note.

## File dự kiến tác động khi implement

- `novel_ai/ui/layout.py`
- `novel_ai/ui/arbiter.py` — thêm pure summary/view model nếu cần
- `tests/unit/test_arbiter.py`
- `tests/integration/test_app_entrypoint.py`
- `README.md`, `docs/user-guide.md`
- contract/spec UI nếu layout mới được chấp thuận

## Acceptance criteria

- [ ] Arbiter mặc định không chiếm cột phải cố định.
- [ ] User mở/đóng Arbiter mà không mất editor input hoặc gọi API.
- [ ] Workspace rộng ra thật sự khi Arbiter đóng.
- [ ] Summary luôn cho biết next step và blocker quan trọng.
- [ ] Recovery/read-only vẫn nhìn thấy khi panel đóng.
- [ ] Nút điều hướng đổi workspace đúng như hiện tại, không chạy action LLM.
- [ ] `arbiter.analyze()` vẫn thuần Python, read-only, không mutate và không gọi LLM.

## Test đề xuất

- Unit: summary count khớp suggestions/stale/recovery của report.
- AppTest: mở/đóng panel không đổi fingerprint project và không tăng call count.
- AppTest: navigation từ suggestion vẫn đổi đúng workspace.
- Manual screenshot với project mới (nhiều suggestion) và project đang viết chapter (ít suggestion).
- Kiểm tra keyboard focus/label của control Arbiter.

## Ngoài phạm vi

- Không biến Arbiter thành agent hoặc auto-run next step.
- Không tự accept, regenerate hay điều phối workflow.
- Không đổi priority/rule backend chỉ để giảm số dòng UI.

## Follow-up — T32 (2026-09-22)

**Đã implement.** Arbiter thành panel **compact, mặc định đóng**: toggle ở toolbar
(`toggle_arbiter_panel`, `KEY_ARBITER_OPEN`/`KEY_ARBITER_TOGGLE`), chỉ chiếm cột khi user mở; bản
compact dùng `badge_summary`/`compact_label` (`novel_ai/ui/arbiter.py`). Cảnh báo
recovery/read-only/stale vẫn hiện ở main **kể cả khi** panel đóng. Vẫn là hàm Python thuần: không
gọi LLM, không tự chạy bước tiếp.

Bằng chứng:

- `tests/integration/test_app_entrypoint.py::test_shell_layout_has_top_nav_project_drawer_and_compact_arbiter`,
  `::test_arbiter_panel_toggle_keeps_state_and_writes_nothing`,
  `::test_blocking_notices_visible_when_drawer_and_arbiter_closed`;
  `tests/unit/test_arbiter.py::test_badge_summary_*`.
- Trạng thái: đạt bằng AppTest; **chưa** có ảnh/visual (mục 6 của
  [fix-acceptance-report](../design/fix-acceptance-report.md)).
- Bàn giao: [T32](../tasks/T32-workspace-shell.md).


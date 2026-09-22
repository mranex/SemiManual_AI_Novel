# BUG-002 — Nút Arbiter không chuyển workspace

## Metadata

| Thuộc tính | Giá trị |
|---|---|
| Trạng thái | Confirmed / chưa sửa |
| Mức độ | Major |
| Ưu tiên đề xuất | P1 |
| Phát hiện | 2026-09-22 |
| Thành phần | `novel_ai/ui/layout.py` |
| Ví dụ | `Chuyển tới Long Plan` |
| Workaround | Chọn Long Plan trực tiếp trên navbar |

## Mô tả

Các nút `Chuyển tới …` trong pane Arbiter nhận click nhưng workspace hiện tại không đổi. Điều hướng trực tiếp bằng navbar ở đầu app vẫn hoạt động.

## Cách tái hiện đã xác nhận

1. Render navbar, workspace ban đầu là `Co-create`.
2. Render một Arbiter suggestion có `workspace="long_plan"`.
3. Click button `novel_ai_arbiter_open_0_generate_long_plan`.
4. Lượt render phát sinh `StreamlitAPIException`:

   ```text
   st.session_state.novel_ai_workspace_nav cannot be modified after the widget
   with key novel_ai_workspace_nav is instantiated.
   ```

5. State quan sát được ngay lúc lỗi:

   ```text
   novel_ai_workspace     = long_plan
   novel_ai_workspace_nav = Co-create
   radio hiển thị         = Co-create
   ```

Ở rerun kế tiếp, `_render_navbar()` đọc radio `Co-create` và ghi `novel_ai_workspace` trở lại `co_create`, nên kết quả cuối cùng đúng với mô tả người dùng: không chuyển tab.

## Kết quả mong đợi

- Click `Chuyển tới Long Plan` đổi navbar sang `Long Plan`.
- Current workspace render page Long Plan trong cùng thao tác.
- Project đang mở và state workspace liên quan được giữ nguyên.
- Không có exception hoặc rerun loop.

## Kết quả thực tế

- Callback bắt đầu chạy và gán `KEY_WORKSPACE = "long_plan"`.
- Gán tiếp widget key `KEY_WORKSPACE_NAV` bị Streamlit từ chối.
- Radio không đổi, workspace bị đồng bộ ngược về lựa chọn cũ.

## Root cause

### Navbar instantiate widget trước

`novel_ai/ui/layout.py:324`–`346` tạo `st.radio(..., key=KEY_WORKSPACE_NAV)` và đồng bộ `KEY_WORKSPACE` từ radio.

Trong `layout.run()`, `_render_navbar()` chạy tại `novel_ai/ui/layout.py:441`–`443`, trước cả ba pane và Arbiter.

### Arbiter sửa widget key quá muộn

`novel_ai/ui/layout.py:397`–`405` xử lý click như sau:

```python
st.session_state[KEY_WORKSPACE] = suggestion.workspace
st.session_state[KEY_WORKSPACE_NAV] = label_for_workspace(suggestion.workspace)
st.rerun()
```

Tại thời điểm dòng gán `KEY_WORKSPACE_NAV` chạy, radio cùng key đã được instantiate trong script run hiện tại. Streamlit 1.41.1 cấm thao tác này.

Guard của dependency nằm ở `.venv/Lib/site-packages/streamlit/runtime/state/session_state.py:502`–`519`: nếu key thuộc widget đã xuất hiện trong run, `__setitem__` raise `StreamlitAPIException`.

## Vì sao nhìn giống “button không có tác dụng”?

Lệnh đầu tiên đã đổi `KEY_WORKSPACE`, nhưng exception xảy ra trước `st.rerun()`. Widget key vẫn giữ `Co-create`. Khi app render lại, navbar coi `Co-create` là lựa chọn hợp lệ và ghi đè workspace về `co_create`. Không có state điều hướng bền nào sống sót.

## Blast radius

- Mọi suggestion Arbiter trỏ tới workspace khác hiện tại.
- Không riêng Long Plan; cùng handler được dùng cho Co-create, Architect, Short Plan, Skeleton, Writer, Review, Reconcile và Revision.
- Nút có thể bị disabled khi không có LLM client, nhưng với client hợp lệ handler vẫn hỏng như trên.
- Không thấy mutation file project; đây là lỗi UI/session state.

## Khoảng trống kiểm thử

`tests/integration/test_app_entrypoint.py:235`–`297` kiểm tra navigation bằng cách gọi trực tiếp:

```python
at.radio(key=layout.KEY_WORKSPACE_NAV).set_value(...)
```

Test này chứng minh navbar hoạt động nhưng không click bất kỳ button `novel_ai_arbiter_open_*` nào. Search toàn test suite không có reference tới key button Arbiter hoặc label `Chuyển tới`.

## Hướng sửa đề xuất cho turn sau

Chưa triển khai. Fix phải thay đổi thời điểm đồng bộ widget state, không catch/nuốt exception.

Các hướng hợp lệ để cân nhắc:

1. Dùng `on_click` callback của button; callback chạy trước script rerun và có thể cập nhật state trước khi radio được instantiate.
2. Ghi một `pending_workspace` riêng; `_render_navbar()` tiêu thụ pending value và cập nhật radio key trước khi tạo widget.
3. Chọn một source of truth cho workspace, tránh hai key có thể lệch nhau.

## Regression test bắt buộc khi sửa

1. AppTest qua entrypoint thật với project có suggestion Long Plan.
2. Click chính button Arbiter, không set radio trực tiếp.
3. Assert không exception.
4. Assert radio là `Long Plan` và `KEY_WORKSPACE == "long_plan"`.
5. Assert caption/current workspace và page Long Plan đã render.
6. Lặp với ít nhất một workspace chapter như Skeleton hoặc Review.
7. Assert điều hướng không ghi file project và không gọi LLM.

## Acceptance criteria

- Mọi nút `Chuyển tới …` đổi đúng navbar và page.
- Không còn widget-state exception.
- Không có state lệch giữa `KEY_WORKSPACE` và `KEY_WORKSPACE_NAV`.
- Navbar trực tiếp vẫn hoạt động.
- Project đang mở không bị đổi; rerun thuần không mutation.

## Trạng thái xử lý trong phiên review

**Chưa sửa theo yêu cầu người dùng.** Chỉ tái hiện và ghi nhận root cause.

## Resolution — T28 (2026-09-22)

**Đã sửa.** `novel_ai/ui/layout.py` điều hướng bằng `navigate_to_workspace()` gắn vào `on_click`
callback của nút Arbiter (và nút toggle `toggle_arbiter_panel()`), nên state workspace đổi **trước**
lần render kế tiếp thay vì sửa `st.session_state` của widget đã instantiate. Không catch rồi bỏ
`StreamlitAPIException`.

Bằng chứng (đợt nghiệm thu T40, 2026-09-22):

- `tests/integration/test_app_entrypoint.py::test_arbiter_button_switches_workspace_navbar_and_page`
  — click nút thật sau khi navbar render: navbar radio, `KEY_WORKSPACE` và page đổi cùng nhau,
  rerun thêm hai lần không bật lại workspace cũ, 0 LLM call, không ghi file.
- `::test_arbiter_navigation_works_without_llm_client`, `::test_arbiter_navigation_reaches_recovery_workspace`.
- `tests/unit/test_arbiter.py` (badge/summary) + `.\.venv\Scripts\python.exe -m pytest -p no:randomly`
  → 658 passed, 1 skipped, 0 failed.

Chi tiết bàn giao: [docs/tasks/T28-arbiter-navigation.md](../tasks/T28-arbiter-navigation.md);
coverage matrix: [docs/design/fix-acceptance-report.md](../design/fix-acceptance-report.md).

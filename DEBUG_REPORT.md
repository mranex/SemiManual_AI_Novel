# Báo cáo debug tổng — review prototype 2026-09-22

## Phạm vi

- Chỉ review và khám phá bug; **không sửa code**.
- Bug đã điều tra: crash sau Accept Short Plan, nút điều hướng Arbiter không hoạt động,
  thiếu `length_guidance` trước khi lập Short Plan, và Long Plan có thể collapse toàn
  planning horizon thành một Arc.
- Không gọi API trả phí, không thay đổi accepted state, không sửa project `ACC`.

## Kết luận

| ID | Mức độ | Trạng thái | Kết luận ngắn |
|---|---|---|---|
| BUG-001 | Blocker / nghiêm trọng | Đã tái hiện, đã xác định root cause | Project tree tạo `st.expander` lồng nhau khi đã có chapter metadata. Short Plan accept chỉ là trigger làm xuất hiện chapter; dữ liệu accept không bị hỏng. |
| BUG-002 | Major | Đã tái hiện, đã xác định root cause | Nút Arbiter sửa state của radio sau khi widget đã được instantiate; Streamlit raise exception, navbar không đổi. |
| BUG-003 | Major / có thể tốn API | Đã tái hiện, có bằng chứng `ACC` | UI gửi `length_guidance` rỗng và backend không guard trước LLM; lỗi chỉ bị phát hiện khi validate output sau API. |
| BUG-004 | High / Major (P1 contract) | Đã tái hiện, đã xác định root cause | Long Plan hỗ trợ nhiều Volume/Arc nhưng default scope, prompt, persistence và validator không bảo đảm horizon cấp truyện; một Arc `1..120`, gap, thiếu đầu/cuối và cả plan rỗng đều có thể được accept. |

Hồ sơ chi tiết:

- [BUG-001](docs/bugs/BUG-001-nested-expanders-after-short-plan-accept.md)
- [BUG-002](docs/bugs/BUG-002-arbiter-navigation-button-does-not-switch-workspace.md)
- [BUG-003](docs/bugs/BUG-003-missing-length-guidance-reaches-short-plan-api.md)
- [BUG-004](docs/bugs/BUG-004-long-plan-horizon-collapses-into-one-arc.md)

## Bằng chứng chính

1. `projects/acc/plans/short_plan.json` đang `accepted`, revision 1, gồm `ch_0001`, `ch_0002`, `ch_0003`.
2. Ba `chapter.json` đều parse được, status `planned`, pin đúng Short Plan revision 1; chuỗi `previous_chapter_id` hợp lệ.
3. `projects/acc/.ops/pending/` không có operation dở.
4. Probe `streamlit.testing.v1.AppTest` qua entrypoint thật `novel_ai/app.py`, mở slug `acc`, tái hiện chính xác:

   ```text
   StreamlitAPIException: Expanders may not be nested inside other expanders.
   ```

5. Probe trực tiếp `project_tree.render(Project.open(..., "acc"))` cũng tái hiện độc lập với page Short Plan, LLM và API.
6. Cây dữ liệu thực tế là:

   ```text
   chapters (group)
   ├── ch_0001 (chapter)
   │   ├── skeleton_ch_0001
   │   └── reconciliation_ch_0001
   ├── ch_0002 (chapter)
   │   ├── skeleton_ch_0002
   │   └── reconciliation_ch_0002
   └── ch_0003 (chapter)
       ├── skeleton_ch_0003
       └── reconciliation_ch_0003
   ```

   Renderer dùng expander cho mọi node có con, nên `chapters` và từng `chapter` trở thành expander lồng nhau.

## Phạm vi ảnh hưởng

- Không riêng dữ liệu `ACC` và không phụ thuộc số chapter là 3.
- Mọi project có ít nhất một `chapter.json` mà project tree dựng chapter cùng hai node con Skeleton/Reconciliation đều có thể crash ngay khi render shell.
- Vì Short Plan accept tạo chapter metadata trong cùng transaction, lần rerun ngay sau accept là thời điểm lỗi thường xuất hiện đầu tiên.
- Restart không khắc phục: chapter metadata đã được lưu hợp lệ trên disk và lần render mới dựng lại cùng cấu trúc lỗi.
- Đây là lỗi availability ở UI. Chưa thấy bằng chứng accepted Short Plan hoặc chapter metadata bị corruption.

## Tóm tắt BUG-002 — Arbiter không chuyển workspace

- Navbar radio được tạo với key `novel_ai_workspace_nav` trước khi Arbiter render.
- Callback nút Arbiter sau đó gán trực tiếp lại chính key này trong cùng script run.
- Streamlit cấm sửa session state của widget đã instantiate và raise `StreamlitAPIException`.
- Probe ghi nhận state trung gian lệch: `novel_ai_workspace = long_plan`, nhưng radio/nav vẫn `Co-create`; rerun kế tiếp lại ghi đè workspace về `co_create`.
- Top navbar hoạt động vì đó là event của chính radio; chỉ đường điều hướng từ Arbiter hỏng.
- Workaround hiện tại: chọn workspace trực tiếp bằng navbar trên cùng.

## Tóm tắt BUG-003 — thiếu `length_guidance`

- Contract yêu cầu object `{language, pov, length_guidance}` có ba chuỗi không rỗng; backend phải chặn yêu cầu viết thiếu trước khi gọi LLM.
- UI chỉ điền mặc định `language`; `pov` và `length_guidance` mặc định rỗng.
- Vì `language` không rỗng, object vẫn được đưa vào `chapter_constraints` và gửi service/API.
- `short_planner.generate()` không validate input constraints trước `complete_json()`.
- Error record thật tại `projects/acc/raw/2026-09-22/op_ui_c0565cffe594ed6b95d26d44_short_plan.error.json` ghi ba blocking issue `invalid_outline_contract_item`, cả ba đều thiếu `length_guidance`.
- Nếu model giữ giá trị rỗng, API đã gọi xong mới sinh `ValidationFailure`; nếu model tự bịa độ dài, output có thể qua structural validation nhưng user không còn kiểm soát contract viết.
- Workaround hiện tại: nhập `length_guidance` (và POV theo contract hiện hành) cho mọi chapter trước khi Generate.

## Tóm tắt BUG-004 — Long Plan collapse planning horizon

- Spec phân tầng `Long Plan = Volume → Arc`, `Short Plan = Arc → Chapter`; yêu cầu mới
  của user làm rõ `planning_scope` là complete planning horizon, có thể bao trùm nhiều
  volume/arc hoặc toàn truyện.
- Runtime model đã hỗ trợ nhiều volume/arc; đây không phải lỗi thiếu array/schema nesting.
- Project mới mặc định `planning_scope=1..3`, suy tiếp từ current chapter, Short Plan và
  chapter metadata; UI prefill và mô tả chính default local/progress-derived này.
- Prompt có hướng dẫn multi-volume/multi-arc nhưng không khóa việc phân rã horizon lớn;
  fixture đầy đủ duy nhất của Long Plan là một volume/một arc `1..7`.
- `_range_issues()` chỉ chặn out-of-scope và overlap. Probe service thật xác nhận one arc
  `1..120`, gap, thiếu coverage hai đầu và `volumes: []` đều được generate rồi accept.
- `planning_scope` chỉ trả trong `ActionResult.data`, không persist cùng revision; Accept
  revalidate bằng `scope=None`. Vì vậy horizon gốc không thể được kiểm tra lại sau reload.
- History project `ACC` có revision đầu `1 volume/1 arc/1..3`, nhưng accepted hiện tại là
  `3 volumes/5 arcs/1..10`. Do đó bug không phải “code luôn ép đúng một arc”; nó là thiếu
  guarantee cộng với default/contract sai abstraction.
- Collapse lan xuống Short Plan vì backend mặc định lấy mọi chapter trong range của arc;
  một arc `1..120` có thể biến thành request lập ChapterPlan cho 120 chương.
- Reply AI trước đúng về hướng root cause và việc không dùng quota cơ học, nhưng nói quá về
  ví dụ prompt, chưa nhận ra scope bị mất trước Accept, và khẳng định “không cần schema” quá
  sớm. Có thể giữ payload Volume/Arc hiện tại, nhưng phải chốt nơi persist horizon.

## Test đã chạy

| Kiểm tra | Kết quả |
|---|---|
| Load/parse Short Plan và ba chapter của `ACC` qua storage | Pass |
| Kiểm tra pending operation của `ACC` | Pass — không có pending operation |
| AppTest trực tiếp `project_tree.render` trên `ACC` | Fail như kỳ vọng — tái hiện BUG-001 |
| AppTest qua entrypoint thật `novel_ai/app.py` trên `ACC` | Fail như kỳ vọng — cùng stack trace BUG-001 |
| AppTest tối thiểu: click `Chuyển tới Long Plan` sau khi navbar đã render | Fail như kỳ vọng — widget state không được phép sửa sau instantiate (BUG-002) |
| Probe UI constraint rỗng → service bằng FakeLLM | Xác nhận một LLM call vẫn xảy ra; candidate có thể được tạo nếu model tự điền (BUG-003) |
| Probe output giữ `length_guidance` rỗng | Một LLM call rồi `ValidationFailure`; raw/error record được lưu (BUG-003) |
| Probe Long Plan one arc `1..120` | Accepted — tái hiện BUG-004 |
| Probe Long Plan gap / thiếu edge coverage / plan rỗng | Đều Accepted — xác nhận thiếu semantic invariant |
| Probe Long Plan overlap / out-of-scope | Đều bị reject — hai guard hiện có hoạt động |
| Test tập trung Long Plan/models/validation | `65 passed` |
| Document checker planning prompts | PASS 9 cases; chỉ kiểm tra tài liệu, không phải runtime validation |
| Toàn bộ `python -m pytest` | `493 passed, 4 failed, 1 skipped` |

Bốn failure của suite hiện tại nằm ở kiểm tra cấu hình LLM mặc định (`test_layout_router.py` và một test trong `test_app_entrypoint.py`), do test đọc cấu hình `.env` thật trong repo. Chúng không phải đường tái hiện BUG-001/BUG-004 và chưa được điều tra thành bug riêng trong phiên này. Quan trọng hơn, suite hiện không có test shell thật với project đã có chapter, nên BUG-001 không bị bắt.

## Khoảng trống kiểm thử dẫn tới lọt bug

- Test shell/project tree hiện chỉ mở project mới, chưa có chapter.
- Test flow Accept Short Plan render riêng workspace bằng harness gọi `layout.render_workspace(ctx)`; nó không render `layout.run()` và không đi qua project pane.
- Vì vậy cả hai nhóm test đều pass riêng lẻ nhưng không kiểm tra giao điểm: **Accept Short Plan → tạo chapter metadata → rerun toàn shell → render project tree**.
- Test navigation chỉ thay giá trị navbar radio trực tiếp; không click nút `novel_ai_arbiter_open_*`.
- Test constraint hiện còn khẳng định payload rỗng có thể bị bỏ qua, và nhiều service test gọi Short Plan không truyền `chapter_constraints`; chưa có test “thiếu length/POV thì zero LLM calls”.
- Helper Long Plan trong integration test chỉ tạo một volume/một arc; happy path còn assert
  default `1..3`. Không có test multi-volume/multi-arc horizon lớn, gap, uncovered start/end,
  empty payload hoặc persist/revalidate horizon qua reload và Accept.
- Static checker của planning fixture chỉ kiểm tra từng arc nằm trong scope, không kiểm tra
  coverage hoặc decomposition.

## Trạng thái thay đổi

- Source code: không thay đổi.
- Project `ACC`: không thay đổi.
- Test: không thêm/sửa.
- Tài liệu debug hiện có: file này và bốn hồ sơ BUG-001/002/003/004 trong `docs/bugs/`.

## Bước tiếp theo đề xuất

BUG-001 vẫn là blocker availability vì khóa project. BUG-004 là ưu tiên P1 kế tiếp vì sai tầng
authority và làm hỏng kiến trúc toàn workflow dù lifecycle vẫn báo valid; cần chốt contract horizon
trước khi patch. BUG-003 gây request API vô ích hoặc để model tự chọn contract viết; BUG-002 còn
workaround qua navbar. Mọi fix cần regression test đúng đường end-to-end, không xóa dữ liệu `ACC`
để né lỗi.

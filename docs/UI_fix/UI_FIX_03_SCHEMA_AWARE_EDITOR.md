# UI-FIX-03 — Editor dễ đọc sau generation

Trạng thái: **đề xuất sau review — chưa implement**  
Ưu tiên: **P0 / Critical UX**  
Dependency UX: UI-FIX-02 cho flow tự động `stream completed → editor`

## Vấn đề

UI hiện có nhiều kiểu xử lý candidate, nhưng chưa có một editor nhất quán cho người viết:

- Architect sửa candidate bằng JSON text area (`novel_ai/pages/architect.py:428-444`).
- Skeleton sửa bằng JSON text area (`novel_ai/pages/skeleton.py:270-323`).
- Reconcile sửa proposal bằng JSON text area (`novel_ai/pages/reconcile.py:190-210`).
- Premise revision cũng dùng JSON text area (`novel_ai/pages/revision.py:562-573`).
- Long Plan chỉ xem `Candidate (dễ đọc) / Accepted / JSON thô`, rồi Accept/Reject (`novel_ai/pages/long_plan.py:211-279`).
- Short Plan có pattern tương tự và không có editor candidate (`novel_ai/pages/short_plan.py:338-399`).
- Prose đã có text area trong Writer/Review, nhưng bị hẹp bởi shell và chưa có Edit/Preview rõ ràng.

Service hiện có `edit_candidate` cho Architect và Skeleton, `edit_reconciliation_candidate` cho Reconcile. Long Plan/Short Plan chưa có action sửa candidate tương ứng, nên đây không chỉ là thay widget.

## Nguyên tắc editor

- Không cần rich text editor; giữ đúng spec mục 18 và MVP boundary.
- Nội dung dành cho người viết phải hiển thị bằng nhãn có nghĩa, không bắt sửa dấu ngoặc JSON.
- Stable ID, revision, status, source, dependency pin là read-only hoặc chọn từ danh sách hợp lệ.
- Raw JSON vẫn tồn tại trong expander/tab `Nâng cao`, dùng cho debug và power user.
- Mỗi Save là action tường minh; không ghi file theo từng keystroke.
- Save chỉ cập nhật candidate/draft và validate lại; Accept/Finalize là action riêng.

## Editor theo loại artifact

### Prose / Base Idea

- Tabs `Edit | Preview`.
- Text area full-width, chiều cao tối thiểu 480 px hoặc theo viewport.
- Hiện word/character count và prose revision.
- Save Draft tạo revision mới như hiện tại; preview không ghi file.

### Premise

- Field riêng: logline, protagonist, goal, stakes, core conflict, constraints và các field đúng schema thật.
- Array dùng `st.data_editor` hoặc danh sách row có Add/Remove rõ ràng.
- Không cho đổi metadata ID/revision bằng text tự do.

### Characters / World Rules / Foreshadow

- Mỗi entity là card/expander có tên hiển thị.
- Stable ID read-only.
- `effective_from_chapter` là number input.
- Author-only field có badge cảnh báo, không lẫn với writer-safe projection.
- Add/Remove/Reorder chỉ tác động working copy; Save mới gọi service.

### Long Plan

- Cấu trúc `Volume → Arc` bằng expander lồng một cấp hoặc tabs theo volume.
- Arc editor có title, goal, conflict, chapter range, relationship direction, reveal/thread.
- Cần thêm service action `edit_candidate(...)` để dùng cùng validation/freshness/snapshot rules với generate.

### Short Plan

- Chọn arc, mỗi chapter là card.
- Các field summary/hook/goal/ending là text input/area.
- `outline` là list row; character/world/thread IDs là multiselect từ accepted index.
- Cần thêm service action `edit_candidate(...)`; không viết trực tiếp `plans/short_plan.json` từ UI.

### Skeleton

- Mỗi section là card: index, type, instruction, purpose, characters, constraints/foreshadow surface.
- Stable section ID read-only.
- Add/Remove/Reorder section trên working copy; Save gọi `skeleton.edit_candidate` hiện có.

### Reconciliation

- Timeline là group field riêng.
- Relationship updates là table/card theo pair stable ID.
- Raw JSON đặt trong `Nâng cao`.
- Save vẫn gọi `edit_reconciliation_candidate` và validate lại.

## Component/contract đề xuất

Tạo registry UI nhỏ theo artifact type, ví dụ:

```text
editor_for("premise")
editor_for("characters")
editor_for("long_plan")
editor_for("short_plan")
editor_for("skeleton")
editor_for("reconciliation")
```

Mỗi editor chỉ chuyển `model → working fields → payload`. Validation chính thức vẫn do service/model hiện có. Không tạo generic form framework lớn; component nhỏ theo schema sẽ dễ hiểu và dễ test hơn.

## File dự kiến tác động khi implement

- helper/editor modules nhỏ dưới `novel_ai/ui/` hoặc `novel_ai/pages/`
- `novel_ai/pages/architect.py`
- `novel_ai/pages/long_plan.py`
- `novel_ai/pages/short_plan.py`
- `novel_ai/pages/skeleton.py`
- `novel_ai/pages/writer.py`
- `novel_ai/pages/review.py`
- `novel_ai/pages/reconcile.py`
- `novel_ai/pages/revision.py`
- `novel_ai/services/long_planner.py` và `short_planner.py` cho edit candidate
- tests UI/service tương ứng

## Acceptance criteria

- [ ] Sau generation complete, candidate/draft mở ngay trong editor dễ đọc.
- [ ] Người dùng hoàn thành happy path mà không cần mở/sửa Raw JSON.
- [ ] Raw JSON vẫn xem/sửa được trong chế độ nâng cao nếu project muốn giữ capability cũ.
- [ ] Long Plan và Short Plan candidate có thể sửa trước Accept.
- [ ] Stable ID/dependency pin/status không bị sửa ngầm bởi form.
- [ ] Save candidate sai schema hiển thị lỗi cạnh field phù hợp và giữ input đang sửa.
- [ ] Save không đồng nghĩa Accept; accepted revision giữ nguyên tới action Accept.
- [ ] Prose edit tiếp tục làm Human Review cũ mất hiệu lực như contract hiện tại.
- [ ] Rerun không làm mất working input và không tự lưu.

## Test đề xuất

- Round-trip cho từng editor: payload → form state → payload không mất field.
- Validation error map đúng field/card, working input vẫn còn.
- Long/Short edit candidate không đổi accepted trước Accept.
- Stable IDs và future `effective_from_chapter` không bị thay ngoài ý muốn.
- Prose Edit/Preview không mutate; Save tạo revision đúng một lần.
- Manual usability: user mới sửa được Premise/Long Plan/Short Plan/Skeleton mà không cần biết JSON.

## Ngoài phạm vi

- Không rich text/WYSIWYG, collaborative editing hoặc selected-text magic.
- Không tự sửa nội dung bằng AI khi user chỉ đang edit.
- Không bỏ Raw JSON/debug view.

## Follow-up — T37/T38/T39 (2026-09-22)

**Đã implement.** `novel_ai/ui/editor.py` (schema-aware, đệ quy, roundtrip giữ field không có widget)
+ form cho Architect/Premise (T37), Long Plan/Short Plan (T38), Skeleton/Reconciliation và prose
Edit|Preview (T39). Raw JSON vẫn ở tab nâng cao và đi qua **cùng** guard service; Save không tự
Accept; metadata app-owned (`*_id`, `chapter_number`, `status`, revision, pin, `planning_scope`,
`source_final_candidate`) read-only.

Bằng chứng:

- `tests/integration/test_t37_foundation_editor.py` (8 test), `test_t38_planning_editor.py` (8 test),
  `test_t39_chapter_editors.py` (11 test).
- Giới hạn còn lại: Long Plan/Short Plan chưa có nút thêm–bỏ arc/chapter; field enum
  (`purpose_visibility`, `writer_context_policy`) chưa có widget nên phải dùng raw JSON. Xem mục 7 của
  [fix-acceptance-report](../design/fix-acceptance-report.md).
- Bàn giao: [T37](../tasks/T37-foundation-editors.md), [T38](../tasks/T38-planning-editors.md),
  [T39](../tasks/T39-chapter-reconcile-editors.md).


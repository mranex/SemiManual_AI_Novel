# UI-FIX-02 — Generation stream ở trung tâm workspace

Trạng thái: **đề xuất sau review — chưa implement**  
Ưu tiên: **P0 / Critical UX**

## Vấn đề

Writer là nơi duy nhất có UI stream: checkbox ở `novel_ai/pages/writer.py:183-190`, callback delta ở `writer.py:207-216`, sau đó rerun ngay ở `writer.py:252-253`. Stream này là chi tiết cục bộ của Writer, chỉ hiện tail trong code block và biến mất khỏi stage sau completion.

Các structured action dùng blocking call:

- helper `complete_json()` gọi `client.complete()` tại `novel_ai/services/co_create.py:383-407`;
- `generate_structured()` gọi `client.complete()` tại `novel_ai/core/llm.py:888-912`;
- Architect, Long Plan và Short Plan dùng `complete_json()`;
- Skeleton, AI Review và một số action khác gọi `client.complete()` trực tiếp;
- Reconcile/impact report dùng `generate_structured()`.

Trong thời gian request chạy, user không có output sống để quan sát.

## UX đích

Mỗi workspace có một **Generation surface** ở đầu vùng nội dung chính:

```text
AI Generation
Action: Generate Short Plan · operation_id …
Status: connecting → streaming → validating → candidate ready

[output tăng dần theo delta]

[Continue/Edit candidate] [Retry] [Discard partial]
```

State hiển thị tối thiểu:

- `idle`: chưa chạy action;
- `connecting`: request đã bắt đầu;
- `streaming`: đang nhận delta;
- `validating`: đã nhận xong, đang parse/validate;
- `completed`: candidate/draft đã lưu;
- `partial`: stream đứt, có raw partial và action recovery phù hợp;
- `error`: lỗi trước khi có delta, accepted state giữ nguyên.

Sau `completed`, editor tương ứng xuất hiện ngay bên dưới. Transcript hoàn tất không được biến mất ngay vì `st.rerun()`.

## Contract kỹ thuật đề xuất

### 1. Event chung, không import Streamlit vào service

Thêm callback/protocol thuần Python, ví dụ:

```python
GenerationEvent(status, text_delta, operation_id, prompt_id, detail)
on_event(event)
```

Service phát event; page quyết định render bằng `st.empty()`/container. Không truyền object UI xuống core.

### 2. Structured streaming

Thêm đường stream có kiểm soát cho structured generation:

1. gọi adapter stream với `json_output=True` nếu provider hỗ trợ;
2. cộng dồn raw text;
3. lưu raw/partial theo storage contract;
4. chỉ parse/validate sau terminal `completed`;
5. chỉ tạo candidate khi payload hoàn chỉnh và validate xong;
6. `partial`/`error` không merge, không auto accept.

Nếu provider không hỗ trợ stream cho structured output, UI phải hiện rõ `non-streaming provider call` cùng lifecycle/elapsed state; không phát delta giả.

### 3. Session state và persistence

- Session state giữ view state/transcript để survive rerun trong phiên.
- Raw final và raw partial có giá trị phục hồi vẫn lưu qua storage hiện có.
- `operation_id` tất định tiếp tục là khóa idempotency.
- Không lưu API key, full prompt secret hoặc author-only context vào UI transcript/log mới.

### 4. Thứ tự nối

1. Writer: thay progress cục bộ bằng generation surface chung.
2. Co-create/Architect/Long/Short/Skeleton.
3. AI Review/Rewrite/Reconcile/Impact report.
4. Fallback non-streaming và failure paths.

## File dự kiến tác động khi implement

- module UI chung mới, ví dụ `novel_ai/ui/generation.py`
- `novel_ai/ui/layout.py` hoặc helper page chung để đặt generation surface
- `novel_ai/core/llm.py`
- `novel_ai/services/co_create.py` và các service gọi LLM
- các page có action LLM trong `novel_ai/pages/`
- unit/integration tests LLM + AppTest UI

Tên module chỉ là đề xuất; không cần thêm layer khác nếu một helper UI và một protocol callback đã đủ.

## Acceptance criteria

- [ ] Mọi action LLM do user bấm có trạng thái quan sát được ở trung tâm.
- [ ] Provider có streaming thì delta hiển thị tăng dần, không chỉ spinner.
- [ ] Completed transcript còn nhìn thấy sau rerun và editor xuất hiện ngay bên dưới.
- [ ] Structured payload chỉ parse/validate sau khi stream complete.
- [ ] Stream đứt lưu partial/raw, không tạo candidate complete, không đổi accepted.
- [ ] Retry cùng `operation_id` không gọi/merge trùng.
- [ ] Chuyển workspace hoặc pure rerun không tự phát request.
- [ ] Không leak prompt/context author-only vào transcript người dùng nếu contract không cho phép.

## Test đề xuất

- Fake client phát 3 delta + completed: UI nhận đúng thứ tự, candidate được tạo một lần.
- Fake client phát 2 delta + interrupted: UI hiện partial, accepted/candidate cũ giữ nguyên.
- Structured JSON bị cắt giữa stream: raw partial còn, validation không chạy như payload complete.
- Provider non-streaming: UI ghi đúng fallback, không giả token.
- Writer complete/partial regression giữ nguyên lifecycle hiện tại.
- AppTest double-click/rerun không tăng số API calls.

## Ngoài phạm vi

- Không background worker, queue hoặc concurrent generation.
- Không nút Stop bắt buộc nếu adapter chưa hỗ trợ cancel.
- Không biến stream thành chat log vĩnh viễn.

## Follow-up — T33/T34/T35 + T40 (2026-09-22)

**Đã implement.** Event contract thuần Python (`novel_ai/core/generation.py`) + surface UI dùng
chung (`novel_ai/ui/generation.py`) cho **mọi** action gọi LLM; service phát event qua
`on_event`/`stream`/`attempt`, UI chỉ báo "candidate ready" ở mốc `saved`. Inventory đầy đủ 12 action
ở [fix-acceptance-report](../design/fix-acceptance-report.md) mục 3. T40 bổ sung surface cho action
phục hồi cuối cùng `revision.reconcile_downstream` (trước đó gọi blocking `complete()`).

Bằng chứng:

- `tests/unit/test_generation_events.py` (state + transition + transcript), `tests/integration/test_t35_stream_coverage.py`,
  `tests/integration/test_t35_ui_surface.py`.
- `tests/integration/test_t40_acceptance.py::test_reconcile_downstream_action_has_generation_surface`
  (transcript `saved` cho `reconcile_downstream`).
- Trạng thái: đạt bằng AppTest; live provider vẫn chưa xác minh (giới hạn ở README mục *Trạng thái và
  giới hạn*).
- Bàn giao: [T33](../tasks/T33-generation-events-writer.md), [T34](../tasks/T34-planning-structured-stream.md),
  [T35](../tasks/T35-chapter-review-stream.md), [T40](../tasks/T40-fix-acceptance-handoff.md).

